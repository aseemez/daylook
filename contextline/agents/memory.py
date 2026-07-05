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

from contextline import config
from contextline.adk_utils import run_text_agent
from contextline.storage.sqlite import Store


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

        from google.adk.agents import LlmAgent

        instruction = (
            "You are the long-term Memory of a personal work-journal. Answer the user's "
            "question about their PAST activity using ONLY the tools to read the database. "
            "Never invent activity, times, or apps — if the tools return nothing for the asked "
            f"date, say there's no record. Today is {today}; resolve relative dates like "
            "'yesterday' against it. Dates are YYYY-MM-DD. Be concise and specific, citing real "
            "times and durations from the data."
        )
        return LlmAgent(
            name="memory",
            model=config.DEFAULT_MODEL,
            instruction=instruction,
            tools=[
                get_journal, get_blocks, list_days,
                time_by_category, time_by_app, find_blocks_by_app,
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
        from contextline.observability import observe

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
