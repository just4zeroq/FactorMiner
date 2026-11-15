"""下载当前币安市场**所有** USDT 永续合约的资金费率历史。

复用项目的 ``materialize_crypto_streams``，因此文件自动落在规范路径
``data/<exchange>/<trade_type>/crypto_streams/<safe_symbol>-1d-funding_rate.feather``，
并保持各合约的**原生结算节律**（8h / 4h / 1h / 12h …）——下载器已在
``crypto_streams.download_funding_rate`` 中解除 8h 写死，按返回时间戳严格分页，不丢行。

每个合约独立容错：单个失败只记入结果 JSON，不中断其余。已落盘的文件会被增量合并
（按 date 去重），可安全重复运行。

用法:
    python scripts/download_all_funding.py --limit 10        # 小批量自测
    python scripts/download_all_funding.py                   # 全量（758 个合约）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Make the project root importable when run as a bare script.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd

from core.data_feed.crypto_streams import (
    crypto_stream_path,
    materialize_crypto_streams,
)
from core.data_feed.data_downloader import DataDownloader


def collect_symbols(exchange) -> list[str]:
    """列出所有活跃的 USDT 结算永续合约 (type=='swap', settle=='USDT')。"""
    exchange.load_markets()
    out = [
        s
        for s, m in exchange.markets.items()
        if m.get("type") == "swap"
        and m.get("settle") == "USDT"
        and m.get("active", True)
    ]
    return sorted(set(out))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--exchange-id", default="binance")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--trade-type", default="futures")
    ap.add_argument("--start", default="2023-01-01", help="回测/下载起点 (naive, UTC)")
    ap.add_argument("--end", default=None, help="终点; 默认=当前 UTC 时间")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个合约 (0=全部); 用于自测")
    ap.add_argument(
        "--results",
        default="user_workspace/reports/funding_download_results.json",
        help="每合约结果的 JSON 输出路径",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="若规范路径下已存在该合约的 funding 文件则跳过 (用于中断后续跑)",
    )
    args = ap.parse_args()

    dl = DataDownloader()
    exchange = dl.get_exchange_instance(
        exchange_id=args.exchange_id, trade_type=args.trade_type
    )
    symbols = collect_symbols(exchange)
    if args.limit:
        symbols = symbols[: args.limit]
    print(f"[init] exchange={args.exchange_id} symbols={len(symbols)} "
          f"start={args.start} end={args.end or 'now'}", flush=True)

    start = pd.Timestamp(args.start).tz_localize(None)
    end = (
        pd.Timestamp(args.end).tz_localize(None)
        if args.end
        else pd.Timestamp.now("UTC").tz_localize(None)
    )

    results: dict[str, dict] = {}
    n = len(symbols)
    for i, sym in enumerate(symbols, 1):
        if args.skip_existing:
            path = crypto_stream_path(
                Path(args.data_root),
                args.exchange_id,
                sym,
                args.timeframe,
                args.trade_type,
                "funding_rate",
            )
            if path.exists():
                results[sym] = {"ok": True, "rows": None, "error": None,
                                "secs": 0.0, "skipped": True}
                if i % 25 == 0 or i == n:
                    ok = sum(1 for v in results.values() if v["ok"])
                    print(f"[{i}/{n}] ok={ok} (skip {sym})", flush=True)
                continue
        t0 = time.time()
        try:
            status = materialize_crypto_streams(
                Path(args.data_root),
                args.exchange_id,
                sym,
                args.timeframe,
                args.trade_type,
                start,
                end,
                streams=["funding_rate"],
                exchange=exchange,
            )
            r = status.get("funding_rate", {})
            results[sym] = {
                "ok": bool(r.get("ok")),
                "rows": r.get("rows"),
                "error": r.get("error"),
                "secs": round(time.time() - t0, 1),
            }
        except Exception as exc:  # noqa: BLE001 - one symbol must not abort the run
            results[sym] = {
                "ok": False,
                "rows": None,
                "error": str(exc)[:240],
                "secs": round(time.time() - t0, 1),
            }
        if i % 25 == 0 or i == n:
            ok = sum(1 for v in results.values() if v["ok"])
            print(f"[{i}/{n}] ok={ok} last={sym} rows={results[sym].get('rows')}", flush=True)

    out_path = Path(args.results)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(results, out_path.open("w"), indent=1, ensure_ascii=False)
    ok_total = sum(1 for v in results.values() if v["ok"])
    print(f"[done] {ok_total}/{n} ok; results -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
