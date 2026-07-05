"""On-disk response cache (see CLAUDE.md "Auth & cost").

Keyed on a hash of the exact model input, so re-running the pipeline or ``eval.py`` against the
same fixture costs nothing after the first run. This is load-bearing during iteration — if a
rerun of the same input makes a real API call, that's a bug.

Deliberately tiny: JSON blobs under ``~/.contextline/cache/<sha256>.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from contextline import config


def make_key(*parts: Any) -> str:
    """A stable sha256 over the given parts. Bytes are hashed as-is; everything else via JSON."""
    h = hashlib.sha256()
    for part in parts:
        if isinstance(part, (bytes, bytearray)):
            h.update(b"\x00b")
            h.update(part)
        else:
            h.update(b"\x00j")
            h.update(json.dumps(part, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()


def _path(key: str) -> Path:
    return config.cache_dir() / f"{key}.json"


def get(key: str) -> Any | None:
    """Return the cached value for ``key``, or None on a miss."""
    p = _path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def set(key: str, value: Any) -> None:
    """Store ``value`` (JSON-serializable) under ``key``."""
    config.ensure_dirs()
    _path(key).write_text(json.dumps(value, default=str))


def has(key: str) -> bool:
    return _path(key).exists()
