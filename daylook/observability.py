"""Structured, replayable logging (build step 7).

Every meaningful agent step appends one JSON record — agent, action, a short input summary, a
short output summary, and latency — to a single newline-delimited log file
(``~/.daylook/daylook.log``). ``eval.py`` and anyone debugging can read the whole run
back as structured data rather than scraping prose.

Usage:

    with observe("analyst", "analyze_batch", f"{len(batch)} events") as rec:
        result = do_work()
        rec["output"] = f"{len(result.blocks)} blocks"
        rec["cache_hit"] = False
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from daylook import config


def _log_file() -> Path:
    return config.log_path()


def log_event(record: dict) -> None:
    """Append one structured record as a JSON line."""
    config.ensure_dirs()
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}
    with open(_log_file(), "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


@contextmanager
def observe(agent: str, action: str, input_summary: str = "", **extra) -> Iterator[dict]:
    """Time a step and log it on exit. Yield a dict the caller fills with 'output'/extras."""
    rec: dict = {"agent": agent, "action": action, "input": input_summary, **extra}
    start = time.perf_counter()
    try:
        yield rec
    finally:
        rec["latency_ms"] = int((time.perf_counter() - start) * 1000)
        log_event(rec)


def read_events(path: Path | None = None) -> list[dict]:
    """Read all structured records back. Empty list if the log doesn't exist."""
    p = Path(path) if path is not None else _log_file()
    if not p.exists():
        return []
    events = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def clear(path: Path | None = None) -> None:
    """Truncate the log (used at the start of an eval run)."""
    p = Path(path) if path is not None else _log_file()
    if p.exists():
        p.unlink()
