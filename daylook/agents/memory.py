"""Memory Agent (build step 5) — long-term memory.

Owns the SQLite ``Store`` and is the *only* path to it. Two responsibilities:

  * **Persist** each processed day (writeup + blocks) — deterministic, no model.
  * **Answer historical questions** — an ADK ``LlmAgent`` whose tools read the database, so
    answers are grounded in stored rows, never invented.

Deliberately holds no in-process pipeline state and shares nothing with the Root Agent beyond
this clean interface — so it can later be extracted into a standalone service (the A2A split
CLAUDE.md describes) by moving one file, not rewiring the pipeline.
"""

from __future__ import annotations

from datetime import date as date_cls
from pathlib import Path

from daylook import config
from daylook.adk_utils import run_text_agent
from daylook.storage.sqlite import Store


class MemoryAgent:
    def __init__(self, store: Store | None = None, db_path: Path | str | None = None):
        self.store = store if store is not None else Store(db_path)

    def close(self) -> None:
        self.store.close()

    # --- persistence (deterministic) ---

    def persist_day(self, day: dict, writeup: str) -> str:
        """Save a day's writeup and blocks. ``day`` is the Analyst day-aggregate dict."""
        the_date = day.get("date")
        if not the_date:
            raise ValueError("day summary is missing a 'date'")
        self.store.save_day(the_date, writeup, day.get("blocks", []))
        return the_date

    # --- retrieval (deterministic) ---

    def get_journal(self, date: str) -> dict | None:
        return self.store.get_day(date)

    def get_blocks(self, date: str) -> list[dict]:
        return self.store.get_blocks(date)

    def available_dates(self) -> list[str]:
        return self.store.list_dates()

    def content_dates(self) -> list[str]:
        """Dates with a writeup or any blocks — the journal list (includes manual-only days)."""
        return self.store.dates_with_content()

    # --- manual calendar edits (user-authored blocks) ---
    # The calendar shows what was captured; these let the user correct/augment it by hand
    # (e.g. add an offline meeting). All go through the store so Memory stays the only DB path.

    def add_block(
        self, date: str, start: str, end: str, label: str,
        app_name: str, category: str | None, duration_seconds: int,
    ) -> dict:
        block_id = self.store.add_block(
            date, start, end, label, app_name, category, duration_seconds, manual=True
        )
        return self.store.get_block(block_id)

    def update_block(self, block_id: int, **fields) -> dict | None:
        self.store.update_block(block_id, **fields)
        return self.store.get_block(block_id)

    def delete_block(self, block_id: int) -> bool:
        return self.store.delete_block(block_id)

    # --- natural-language answering (LLM + DB tools) ---

    def _make_query_agent(self, today: str):
        store = self.store

        def get_journal(date: str) -> dict:
            """Return the saved daily writeup record for a date (YYYY-MM-DD), or {} if none."""
            return store.get_day(date) or {}

        def get_blocks(date: str) -> list:
            """Return the labeled activity blocks for a date (YYYY-MM-DD)."""
            return store.get_blocks(date)

        def list_days() -> list:
            """Return every date (YYYY-MM-DD) that has a saved journal."""
            return store.list_dates()

        def time_by_category(date: str) -> dict:
            """Return seconds spent per activity category on a date (YYYY-MM-DD)."""
            return store.time_by_category(date)

        def time_by_app(date: str) -> dict:
            """Return seconds spent per app on a date (YYYY-MM-DD)."""
            return store.time_by_app(date)

        def find_blocks_by_app(app_name: str) -> list:
            """Return all blocks across all days for a given app name."""
            return store.find_blocks_by_app(app_name)

        def productivity_summary(date: str) -> dict:
            """Seconds spent PRODUCTIVE vs DISTRACTING vs NEUTRAL on a date (YYYY-MM-DD).

            Rolls each block's category into a productivity lane so you can answer
            'was I productive?' or 'how much did I waste' honestly, grounded in real data."""
            agg = {"productive": 0, "distracting": 0, "neutral": 0, "by_lane": {}}
            for cat, secs in store.time_by_category(date).items():
                lane = config.lane_of(cat)
                agg["by_lane"][lane] = agg["by_lane"].get(lane, 0) + secs
                agg[config.productivity_of(cat)] += secs
            return agg

        from google.adk.agents import LlmAgent

        instruction = (
            "You are the long-term Memory of a personal work-journal. Answer the user's "
            "question about their PAST activity using ONLY the tools to read the database. "
            "Never invent activity, times, or apps — if the tools return nothing for the asked "
            f"date, say there's no record. Today is {today}; resolve relative dates like "
            "'yesterday' against it. Dates are YYYY-MM-DD. Be concise and specific, citing real "
            "times and durations from the data.\n"
            "When the user asks whether they were productive, focused, or wasted time, call "
            "productivity_summary: 'productive' covers focused work, research and comms; "
            "'distracting' covers social/feeds/entertainment; 'neutral' is breaks/idle. Judge "
            "honestly from those numbers rather than guessing, and it's fine to gently point out "
            "a distracting stretch (e.g. time on social apps) if the data shows it."
        )
        return LlmAgent(
            name="memory",
            model=config.DEFAULT_MODEL,
            instruction=instruction,
            tools=[
                get_journal, get_blocks, list_days,
                time_by_category, time_by_app, find_blocks_by_app,
                productivity_summary,
            ],
        )

    def answer(
        self,
        question: str,
        today: str | None = None,
        refinements: list[str] | None = None,
    ) -> str:
        """Answer a natural-language question about stored history.

        ``refinements`` are the current session's user preferences; when present they're
        threaded into the question so answers honor them (e.g. excluding an app).
        """
        from daylook.observability import observe

        today = today or date_cls.today().isoformat()
        agent = self._make_query_agent(today)
        message = question
        if refinements:
            prefs = "\n".join(f"- {r}" for r in refinements)
            message = f"{question}\n\n(Honor these active preferences in your answer:\n{prefs})"
        with observe("memory", "answer", question[:80]) as rec:
            answer = run_text_agent(agent, message).strip()
            rec["output"] = f"{len(answer)} chars"
        return answer
