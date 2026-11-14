"""Download and persist crypto-native streams for local futures pairs.

Examples
--------
Materialise funding + basis + open interest for every pair that already has a
1d OHLCV file::

    python user_workspace/experiment_tools/materialize_crypto_streams.py \
        --timeframe 1d --streams funding_rate,basis,open_interest

Only pairs with a local OHLCV file are considered by default; pass ``--pairs``
to override.  Each stream is independent: a failure (e.g. open interest beyond
Binance's ~30-day retention) is reported but never aborts the rest.
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from core.data_feed.crypto_streams import materialize_crypto_streams
from core.data_feed.data_downloader import DataDownloader
from core.data_feed.naming import safe_symbol


def local_pairs(data_root: Path, exchange: str, timeframe: str, trade_type: str) -> list[str]:
    pattern = data_root / exchange / trade_type / f"*-{timeframe}-{trade_type}.feather"
    pairs = []
    for path in sorted(glob.glob(str(pattern))):
        stem = Path(path).stem
        safe = stem.rsplit("-", 2)[0]
        parts = safe.split("_")
        if trade_type == "futures" and len(parts) >= 3:
            pairs.append(f"{parts[0]}/{parts[1]}:{parts[2]}")
        else:
            pairs.append(f"{parts[0]}/{parts[1]}")
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--exchange", default="binance")
    ap.add_argument("--trade-type", default="futures")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--streams", default="funding_rate,basis,open_interest")
    ap.add_argument("--pairs", default="", help="comma-separated CCXT symbols")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--oi-days", type=int, default=25,
                    help="open interest is only retained ~30d by Binance")
    args = ap.parse_args()

    data_root = Path(args.data_root)
    streams = [s.strip() for s in args.streams.split(",") if s.strip()]
    pairs = (
        [p.strip() for p in args.pairs.split(",") if p.strip()]
        if args.pairs
        else local_pairs(data_root, args.exchange, args.timeframe, args.trade_type)
    )
    if not pairs:
        print("No pairs found. Download OHLCV first or pass --pairs.")
        return 1

    now = pd.Timestamp.now("UTC").tz_localize(None)
    end = pd.Timestamp(args.end) if args.end else now
    oi_start = max(pd.Timestamp(args.start), now - pd.Timedelta(days=args.oi_days))

    dl = DataDownloader()
    exf = dl.get_exchange_instance(exchange_id=args.exchange, trade_type="futures")
    exs = (
        dl.get_exchange_instance(exchange_id=args.exchange, trade_type="spot")
        if "basis" in streams else None
    )

    rows = []
    for pair in pairs:
        # Open interest needs the recent window; other streams use the full range.
        for group in (["funding_rate", "basis"], ["open_interest"]):
            group = [s for s in streams if s in group]
            if not group:
                continue
            start = oi_start if group == ["open_interest"] else pd.Timestamp(args.start)
            status = materialize_crypto_streams(
                data_root, args.exchange, pair, args.timeframe, args.trade_type,
                start, end, streams=group, exchange=exf, spot_exchange=exs,
            )
            for stream, st in status.items():
                rows.append({
                    "pair": pair,
                    "stream": stream,
                    "ok": st["ok"],
                    "rows": st["rows"],
                    "error": (st["error"] or "")[:70],
                })

    df = pd.DataFrame(rows)
    if df.empty:
        print("Nothing requested.")
        return 1
    print("\n=== crypto stream materialisation ===")
    print(df.to_string(index=False))
    ok = df[df["ok"]].groupby("stream").size().to_dict()
    print("\nStored:", ok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
