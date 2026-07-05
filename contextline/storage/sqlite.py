"""SQLite data layer — Contextline's long-term memory.

A thin, dependency-free store over the schema in CLAUDE.md (``days`` + ``blocks``). It knows
nothing about agents or models — just rows in and rows out — so the Memory Agent that wraps it
can later be lifted into its own service without dragging pipeline state along.

``source_event_ids`` is persisted as a JSON array; it's how ``eval.py`` proves every block
traces back to real captured events.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from contextline import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    date       TEXT PRIMARY KEY,
    writeup    TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date             TEXT NOT NULL,
    start            TIMESTAMP NOT NULL,
    end              TIMESTAMP NOT NULL,
    label            TEXT NOT NULL,
    app_name         TEXT NOT NULL,
    category         TEXT,
    duration_seconds INTEGER,
    source_event_ids TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_date ON blocks(date);
"""


class Store:
    """Row-level access to the journal database. One instance owns one connection."""

    def __init__(self, path: Path | str | None = None):
        self.path = str(path) if path is not None else str(config.sqlite_path())
        if self.path != ":memory:":
            config.ensure_dirs()
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- writes ---

    def save_day(self, date: str, writeup: str, blocks: list[dict]) -> None:
        """Upsert a day's writeup and replace its blocks. Idempotent per date."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.conn:  # transaction
            self.conn.execute(
                "INSERT INTO days(date, writeup, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET writeup=excluded.writeup, "
                "created_at=excluded.created_at",
                (date, writeup, now),
            )
            self.conn.execute("DELETE FROM blocks WHERE date = ?", (date,))
            self.conn.executemany(
                "INSERT INTO blocks(date, start, end, label, app_name, category, "
                "duration_seconds, source_event_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        date,
                        b["start"],
                        b["end"],
                        b["label"],
                        b["app_name"],
                        b.get("category"),
                        b.get("duration_seconds"),
                        json.dumps(b.get("source_event_ids", [])),
                    )
                    for b in blocks
                ],
            )

    # --- reads ---

    def get_day(self, date: str) -> dict | None:
        row = self.conn.execute(
            "SELECT date, writeup, created_at FROM days WHERE date = ?", (date,)
        ).fetchone()
        return dict(row) if row else None

    def get_blocks(self, date: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT date, start, end, label, app_name, category, duration_seconds, "
            "source_event_ids FROM blocks WHERE date = ? ORDER BY start", (date,)
        ).fetchall()
        return [self._block_row(r) for r in rows]

    def list_dates(self) -> list[str]:
        rows = self.conn.execute("SELECT date FROM days ORDER BY date").fetchall()
        return [r["date"] for r in rows]

    def time_by_category(self, date: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT category, SUM(duration_seconds) AS secs FROM blocks "
            "WHERE date = ? GROUP BY category", (date,)
        ).fetchall()
        return {r["category"]: int(r["secs"] or 0) for r in rows}

    def time_by_app(self, date: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT app_name, SUM(duration_seconds) AS secs FROM blocks "
            "WHERE date = ? GROUP BY app_name ORDER BY secs DESC", (date,)
        ).fetchall()
        return {r["app_name"]: int(r["secs"] or 0) for r in rows}

    def find_blocks_by_app(self, app_name: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT date, start, end, label, app_name, category, duration_seconds, "
            "source_event_ids FROM blocks WHERE app_name = ? ORDER BY date, start",
            (app_name,),
        ).fetchall()
        return [self._block_row(r) for r in rows]

    @staticmethod
    def _block_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["source_event_ids"] = json.loads(d["source_event_ids"])
        return d
