"""``experiment --sweep`` — declare a multi-hypothesis grid and shard it (F6/L2.2).

A sweep is the honest way to search more than one region: the dimensions are
declared *before* any run, every cell becomes its own reproducible run, and each
cell gets its own pre-registered trial family so "we searched 3 horizons" does
not silently deflate against "we searched one horizon three times deeper".

Sharding exists because a single machine tops out around 1.7x: eight independent
processes over eight cells beat eight threads inside one cell by a wide margin.

sweep.json::

    {
      "name": "crypto_1d_h5",
      "grid": {
        "target.horizon_bars": [5, 20],
        "rebalance_every": [1, 5]
      },
      "seeds": [20261114, 20261115],
      "set": ["fitness.turnover_max=0.15"]
    }
"""

from __future__ import annotations

import copy
import itertools
import json
import os
from typing import Any, Dict, List, Optional, Sequence

from core.config.overrides import OverrideError, apply_overrides


class SweepError(ValueError):
    """The sweep declaration cannot be expanded."""


def load_sweep(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise SweepError(f"Cannot read sweep file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SweepError(f"Sweep file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SweepError(f"Sweep file {path} must contain a JSON object")
    return data


def _label(path: str, value: Any) -> str:
    key = path.split(".")[-1]
    text = str(value)
    for bad in ("/", "\\", " ", ":", "."):
        text = text.replace(bad, "-")
    return f"{key}={text}"


def expand(
    base_config: Dict[str, Any],
    sweep: Dict[str, Any],
    *,
    default_family: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Expand a sweep declaration into concrete run configurations.

    Each entry carries ``label``, ``family`` and ``assignments`` so the caller
    can name the run, pre-register the trial family, and print what was tried.
    """
    grid = sweep.get("grid") or {}
    if not isinstance(grid, dict):
        raise SweepError("sweep.grid must be an object of path -> [values]")
    paths = list(grid)
    values: List[Sequence[Any]] = []
    for path in paths:
        options = grid[path]
        if not isinstance(options, (list, tuple)) or not options:
            raise SweepError(f"sweep.grid[{path!r}] must be a non-empty list")
        values.append(list(options))

    common = list(sweep.get("set") or [])
    seeds = sweep.get("seeds")
    if seeds is not None and (not isinstance(seeds, (list, tuple)) or not seeds):
        raise SweepError("sweep.seeds must be a non-empty list when present")
    seed_list: List[Optional[Any]] = list(seeds) if seeds else [None]

    name = str(sweep.get("name") or "sweep")
    cells: List[Dict[str, Any]] = []
    combos = list(itertools.product(*values)) if paths else [()]
    # A sweep cell is its own hypothesis: if the grid actually varies, each cell
    # must get its own trial family, otherwise "we searched 3 horizons" would be
    # charged as "one horizon three times deeper".
    varies = len(combos) > 1
    for combo in combos:
        assignments: List[str] = []
        dims: List[str] = []
        for path, value in zip(paths, combo):
            payload = json.dumps(value)
            assignments.append(f"{path}={payload}")
            dims.append(_label(path, value))
        label = "_".join([name] + dims)
        if len(cells) and not paths:
            label = f"{name}_{len(cells)}"
        for seed in seed_list:
            cell_assignments = list(common) + list(assignments)
            cell_label = label
            if seed is not None:
                cell_assignments.append(f"experiment.seed={json.dumps(seed)}")
                cell_label = f"{label}_seed{seed}"
            try:
                config, _ = apply_overrides(base_config, cell_assignments)
            except OverrideError as exc:
                raise SweepError(str(exc)) from exc
            exp = config.setdefault("experiment", {})
            declared = exp.get("trial_family") or default_family
            if declared and varies:
                family = f"{declared}:{'_'.join(dims)}"
            else:
                family = declared or cell_label
            exp["trial_family"] = family
            exp.setdefault("family_declared_at", sweep.get("declared_at"))
            cells.append({
                "label": cell_label,
                "family": exp["trial_family"],
                "assignments": cell_assignments,
                "config": config,
            })
    return cells


def select_shard(cells: List[Dict[str, Any]], shard: Optional[str]) -> List[Dict[str, Any]]:
    """Parse ``i/n`` (1-based) and return this shard's cells."""
    if not shard:
        return cells
    try:
        index_text, total_text = str(shard).split("/")
        index, total = int(index_text), int(total_text)
    except (ValueError, AttributeError) as exc:
        raise SweepError(f"--shard must look like i/n, got {shard!r}") from exc
    if total <= 0 or not 1 <= index <= total:
        raise SweepError(f"--shard {shard!r} is out of range")
    return [cell for position, cell in enumerate(cells) if position % total == index - 1]


def describe(cells: List[Dict[str, Any]]) -> str:
    lines = [f"{len(cells)} cell(s):"]
    for cell in cells:
        lines.append(f"  {cell['label']}  family={cell['family']}")
    return "\n".join(lines)


__all__ = ["SweepError", "describe", "expand", "load_sweep", "select_shard"]
