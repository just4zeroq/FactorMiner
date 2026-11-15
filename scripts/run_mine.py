"""DEPRECATED — use ``core.cli pipeline`` instead.

This wrapper used to implement its own refresh loop (one ``core.cli download``
call per symbol) and then launch ``core.cli experiment`` directly. That made it
a *second* definition of the operating discipline, and the two drifted:

* it never ran the preflight, so a short/stale/misconfigured panel was only
  discovered after an hour of mining;
* it refreshed OHLCV only, leaving declared ``crypto_streams`` (funding, open
  interest, basis) silently stale -- ``_attach_crypto_streams`` skips missing
  files, so those columns would attach from an old file or not at all;
* it produced no machine-readable summary, so a scheduler could only tell
  "something exited non-zero" from "everything worked".

``core.cli pipeline`` does refresh -> doctor -> experiment in that order, writes
``pipeline_<ts>.json``, and returns one exit code. This file is now a shim that
forwards to it so existing crontabs keep working, with a loud deprecation notice.

Usage (unchanged)::

    venv/Scripts/python.exe scripts/run_mine.py \
        --config user_workspace/configs/configExperiment.json

    venv/Scripts/python.exe scripts/run_mine.py --skip-refresh   # offline / dev

Equivalent, and preferred::

    venv/Scripts/python.exe -m core.cli pipeline \
        --config user_workspace/configs/configExperiment.json
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.name == "nt":
    _PY = os.path.join(_PROJECT_ROOT, "venv", "Scripts", "python.exe")
else:
    _PY = os.path.join(_PROJECT_ROOT, "venv", "bin", "python")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--config",
        default=os.path.join("user_workspace", "configs", "configExperiment.json"),
        help="mining config (default: user_workspace/configs/configExperiment.json)",
    )
    ap.add_argument("--user-dir", default="user_workspace", help="workspace directory")
    ap.add_argument(
        "--skip-refresh",
        action="store_true",
        help="do NOT refresh data first (offline / dev only)",
    )
    ap.add_argument("--data-root", default=None, help="override data_feeds.data_root")
    ap.add_argument(
        "--summary-dir",
        default=os.path.join("user_workspace", "reports"),
        help="where pipeline_<ts>.json is written",
    )
    args, passthrough = ap.parse_known_args(argv)

    print(
        "[run_mine] DEPRECATED: forwarding to `core.cli pipeline` "
        "(refresh -> doctor -> experiment + JSON summary). Point your scheduler at "
        "that command directly; this shim will be removed later.",
        file=sys.stderr,
        flush=True,
    )

    cmd = [
        _PY, "-m", "core.cli", "pipeline",
        "--config", args.config,
        "--user-dir", args.user_dir,
        "--summary-dir", args.summary_dir,
    ]
    if args.skip_refresh:
        cmd.append("--skip-refresh")
    if args.data_root:
        cmd += ["--data-root", args.data_root]
    cmd += list(passthrough)
    print(">>> " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=_PROJECT_ROOT, env=dict(os.environ)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
