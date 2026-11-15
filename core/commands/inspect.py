import argparse
import json
import logging
import os
import sys

from core.inspector.engine import FactorInspectorEngine
from core.utils.dynamic_loader import load_user_modules
from core.evaluation import holdout_guard

logger = logging.getLogger("FactorInspectorCLI")


def parse_args():
    parser = argparse.ArgumentParser(description="FactorMiner V4 CLI - Factor Inspector Engine")
    parser.add_argument("--factor", type=str, default=None, help="Factor ID stored in factor_db")
    parser.add_argument("--ast", type=str, default=None, help="Raw AST string or dictionary literal")
    parser.add_argument("--code", type=str, default=None, help="Python factor code string")
    parser.add_argument("--config", type=str, default=None, help="Optional: Path to base JSON configuration file")
    parser.add_argument("--pairs", type=str, default=None, help="Comma-separated symbols (e.g. BTC/USDT:USDT,ETH/USDT:USDT)")
    parser.add_argument("--start", type=str, default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--timeframe", type=str, default=None, help="Timeframe (e.g. 5m, 1h, 1d)")
    parser.add_argument("--user-dir", type=str, default="user_workspace", help="Path to user workspace directory")
    parser.add_argument(
        "--allow-holdout-reuse",
        action="store_true",
        help="Permit inspecting an already-revealed holdout window (audited).",
    )
    parser.add_argument(
        "--holdout-mode", type=str, default=None,
        choices=list(holdout_guard.VALID_MODES),
        help="Override the holdout guard mode (default: strict)",
    )
    return parser.parse_args()


def run_inspector(args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # 1. 动态加载用户自定义算子与模型
    if os.path.exists(args.user_dir):
        logger.info("Loading user modules from %s...", args.user_dir)
        load_user_modules(args.user_dir)

    base_config = {}
    if args.config:
        # K-04 / S-08: a missing or corrupt --config silently fell back to the
        # engine's hard-coded BTC/USDT 5m defaults and produced a report that had
        # nothing to do with the requested configuration. Fail loudly instead.
        if not os.path.exists(args.config):
            logger.error("Base config not found: %s", args.config)
            return 1
        try:
            with open(args.config, "r", encoding="utf-8-sig") as f:
                base_config = json.load(f)
        except Exception as e:
            logger.error("Failed to load base config %s: %s", args.config, e)
            return 1
        if not isinstance(base_config, dict):
            logger.error("Base config %s must be a JSON object", args.config)
            return 1

    # 2. 解析 CLI 命令行参数覆盖
    pairs_list = [p.strip() for p in args.pairs.split(",")] if args.pairs else None
    # K-06: a one-sided window used to collapse to ``periods=None`` and take the
    # guard's "unbounded: warning only" branch -- a complete bypass of the
    # one-time holdout ledger. Require both bounds, or neither.
    if bool(args.start) != bool(args.end):
        logger.error(
            "Both --start and --end must be supplied together (got start=%r, "
            "end=%r). A one-sided window cannot be checked against burned "
            "holdout periods.", args.start, args.end,
        )
        return 2
    periods = [[args.start, args.end]] if (args.start and args.end) else None

    # P0-b: the inspector reads realised returns too, so it must not be a
    # back door around the one-time holdout ledger.
    try:
        holdout_guard.guard_periods(
            periods,
            purpose="explore",
            config=base_config,
            mode=getattr(args, "holdout_mode", None),
            allow_reuse=getattr(args, "allow_holdout_reuse", False),
            source="cli:inspect",
            note=f"factor={args.factor or args.ast or args.code or 'inline'}",
        )
    except holdout_guard.HoldoutReuseError as e:
        logger.error("Holdout guard blocked this inspection: %s", e)
        return 1
    except ValueError as e:
        logger.error("Invalid inspection window: %s", e)
        return 2

    # 3. 运行引擎
    engine = FactorInspectorEngine(base_config=base_config)
    try:
        engine.inspect(
            factor_id=args.factor,
            ast_str=args.ast,
            code_str=args.code,
            pairs=pairs_list,
            periods=periods,
            timeframe=args.timeframe,
        )
    except Exception as e:
        logger.error("Inspection failed: %s", e, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    args = parse_args()
    sys.exit(run_inspector(args))
