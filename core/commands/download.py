import argparse
import logging
import asyncio


def _make_downloader():
    """Import lazily: ``batch_downloader`` pulls in ccxt, which is not needed
    for ``--help`` / parsing or for callers that only inspect the CLI surface.
    """
    from core.data_feed.batch_downloader import SmartBatchDownloader
    return SmartBatchDownloader()

def parse_args():
    parser = argparse.ArgumentParser(description="FactorMiner V4 CLI - Data Downloader")
    parser.add_argument("--exchange", type=str, default="binance", help="Exchange name (e.g., binance)")
    parser.add_argument("--symbols", type=str, required=True, help="Comma-separated symbols (e.g., BTC/USDT,ETH/USDT)")
    parser.add_argument("--timeframes", type=str, default="1d", help="Comma-separated timeframes (e.g., 1m,1h,1d)")
    parser.add_argument("--type", type=str, default="spot", choices=["spot", "futures"], help="Market type (spot or futures)")
    parser.add_argument("--start", type=str, required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--mode", type=str, default="full", choices=["full", "incremental", "overwrite"], help="Download mode (full, incremental, or overwrite)")
    return parser.parse_args()

async def run_downloader(args):
    """Download every requested symbol/timeframe and return a shell exit code.

    ``download_ohlcv_batch`` reports failures by returning ``{'success': False}``
    rather than raising, so the caller has to inspect the payload. Previously the
    result was discarded and the command always exited 0 even when nothing was
    written.
    """
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    symbols = [s.strip() for s in args.symbols.split(",")]
    timeframes = [t.strip() for t in args.timeframes.split(",")]

    downloader = _make_downloader()
    failures = []

    for symbol in symbols:
        for timeframe in timeframes:
            label = f"{symbol} {timeframe}"
            logging.info(f"Downloading {label} ({args.type}) from {args.start} to {args.end} (mode: {args.mode})...")
            try:
                result = await asyncio.to_thread(
                    downloader.download_ohlcv_batch,
                    exchange_id=args.exchange,
                    symbol=symbol,
                    timeframe=timeframe,
                    start_date=args.start,
                    end_date=args.end,
                    trade_type=args.type,
                    progress_callback=None,
                    download_mode=args.mode
                )
            except Exception as exc:
                logging.error("Download failed for %s: %s", label, exc)
                failures.append(f"{label}: {exc}")
                continue

            if isinstance(result, dict) and result.get("success") is False:
                reason = result.get("error") or "unknown error"
                logging.error("Download failed for %s: %s", label, reason)
                failures.append(f"{label}: {reason}")

    if failures:
        logging.error(
            "Batch download completed with %d failure(s): %s",
            len(failures), "; ".join(failures),
        )
        return 1

    logging.info("Batch download completed.")
    return 0

if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(asyncio.run(run_downloader(args)))
