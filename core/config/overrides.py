"""Dotted-path config overrides for the CLI (F7).

Changing ``gp.max_depth`` used to mean editing JSON by hand, which also meant
the edit escaped the run manifest's ``config_hash`` whenever someone forgot to
save. ``--set gp.max_depth=5`` applies the change in memory *before* the hash is
computed, so the manifest always describes what actually ran.

Values are parsed as JSON when possible (``5``, ``true``, ``[1,2]``), otherwise
kept as strings. Unknown paths are rejected: silently creating ``gp.maxdepth``
would produce a run that looks configured but is not.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, Iterable, List, Sequence, Tuple


class OverrideError(ValueError):
    """The override path or value cannot be applied safely."""


def _parse_value(raw: str) -> Any:
    text = raw.strip()
    if text == "":
        return ""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def apply_overrides(
    config: Dict[str, Any],
    assignments: Sequence[str],
    *,
    allow_new_keys: bool = True,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Apply ``a.b.c=value`` assignments to a deep copy of ``config``.

    Returns ``(new_config, applied)`` where ``applied`` records every change so
    the run summary can print exactly what was overridden.
    """
    out = copy.deepcopy(config)
    applied: List[Dict[str, Any]] = []
    for item in assignments or []:
        if "=" not in item:
            raise OverrideError(
                f"--set expects key.path=value, got {item!r}"
            )
        path, raw = item.split("=", 1)
        keys = [k for k in path.strip().split(".") if k]
        if not keys:
            raise OverrideError(f"--set path is empty in {item!r}")
        value = _parse_value(raw)
        node = out
        for key in keys[:-1]:
            child = node.get(key)
            if child is None:
                if not allow_new_keys:
                    raise OverrideError(f"Unknown config path {path!r} (no {key!r})")
                child = {}
                node[key] = child
            if not isinstance(child, dict):
                raise OverrideError(
                    f"Cannot descend into {key!r}: it holds a {type(child).__name__}"
                )
            node = child
        leaf = keys[-1]
        if leaf not in node and not allow_new_keys:
            raise OverrideError(f"Unknown config path {path!r}")
        previous = node.get(leaf)
        node[leaf] = value
        applied.append({"path": path.strip(), "previous": previous, "value": value})
    return out, applied


def format_applied(applied: Iterable[Dict[str, Any]]) -> str:
    lines = []
    for item in applied:
        prev = item["previous"]
        lines.append(
            f"  {item['path']}: {prev!r} -> {item['value']!r}"
            if prev is not None
            else f"  {item['path']}: (new) -> {item['value']!r}"
        )
    return "\n".join(lines)


__all__ = ["OverrideError", "apply_overrides", "format_applied"]
