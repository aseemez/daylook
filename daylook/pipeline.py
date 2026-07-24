"""The processing pipeline: capture -> analyst -> journalist -> memory.

One place that runs a day end-to-end, shared by the Root Agent, ``eval.py``, and the FastAPI
app so they can't drift. The per-agent model calls are injectable, which lets tests drive the
whole orchestration deterministically without touching the model provider.

Pending events are processed **per calendar day**. This matters when a backlog piles up (the
app sat unopened for a few days, or an update restarted it while events were waiting): each
day's events become that day's journal, saved under that day's date. Before this, everything
in ``pending/`` was aggregated under the first event's date — three days of backlog would be
filed as one day and silently overwrite that day's real journal.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from pathlib import Path
from typing import Callable

from daylook.agents.analyst import aggregate_day, analyze_batches, summarize_day
from daylook.agents.capture import CaptureAgent, batch_events
from daylook.agents.journalist import write_journal
from daylook.agents.memory import MemoryAgent
from daylook.observability import observe

# One run at a time, process-wide. Two things can now trigger processing concurrently — a
# user's "what did I do today" and the hourly auto-journal thread — and both read-then-move
# the same pending/ files. Module-level (not per-Pipeline) because each caller builds its own
# Pipeline instance; without a shared lock the same events could be double-ingested or a
# half-moved batch re-read mid-move.
_PROCESS_LOCK = threading.Lock()


class Pipeline:
    def __init__(
        self,
        memory: MemoryAgent,
        analyze_fn: Callable = analyze_batches,
        journal_fn: Callable = write_journal,
    ):
        self.memory = memory
        self._analyze = analyze_fn
        self._journal = journal_fn

    def process_day(
        self,
        pending_dir: Path | None = None,
        refinements: list[str] | None = None,
        use_cache: bool = True,
        move_processed: bool = True,
    ) -> dict:
        """Ingest pending events, analyze, write the journal(s), persist. Returns a result dict.

        Events are grouped by calendar date and each date is processed independently —
        batched, analyzed, journaled, and persisted under its own date — so a multi-day
        backlog can never collapse into (or overwrite) a single day. Each day's files are
        moved to ``processed/`` only after that day persisted, so a crash mid-backlog leaves
        the untouched days pending for the next run.

        The returned dict describes the most recent day processed (the one the user usually
        asked about) and lists every day under ``"days"``.

        ``refinements`` (session-scoped user preferences) are threaded into the Journalist.
        ``move_processed`` moves ingested events to ``processed/`` after a successful persist;
        set False to keep a fixture reusable across runs (e.g. in eval).
        """
        with _PROCESS_LOCK:
            return self._process_day_locked(
                pending_dir=pending_dir, refinements=refinements,
                use_cache=use_cache, move_processed=move_processed,
            )

    def _process_day_locked(
        self,
        pending_dir: Path | None = None,
        refinements: list[str] | None = None,
        use_cache: bool = True,
        move_processed: bool = True,
    ) -> dict:
        capture = CaptureAgent(pending=pending_dir)
        with observe("capture", "batch_pending") as rec:
            events = capture.read_pending()
            by_date: dict[str, list] = defaultdict(list)
            for e in events:
                by_date[e.dt.date().isoformat()].append(e)
            rec["input"] = f"{len(events)} events"
            rec["output"] = f"{len(by_date)} day(s)"
        if not events:
            return {
                "date": None,
                "writeup": "No pending activity to process.",
                "day": {"date": None, "blocks": []},
                "num_blocks": 0,
                "num_events": 0,
                "days": [],
            }

        day_results: list[dict] = []
        for date in sorted(by_date):
            # Batching happens inside the day, so a batch can never span midnight even if the
            # user worked straight through it — the journal is a per-date artifact.
            batches = batch_events(by_date[date])

            with observe("analyst", "analyze_batches", f"{date}: {len(batches)} batches") as rec:
                analyses = self._analyze(batches, pending_dir=capture.pending, use_cache=use_cache)
                rec["output"] = f"{sum(len(a.blocks) for a in analyses)} blocks"

            day = aggregate_day(analyses)
            day["date"] = date  # authoritative: the grouping key, not the first batch's guess
            day = self._merge_with_stored(date, day)

            with observe("journalist", "write_journal", f"{date}: {len(day['blocks'])} blocks") as rec:
                writeup = self._journal(day, refinements=refinements, use_cache=use_cache)
                rec["output"] = f"{len(writeup)} chars"
                rec["refinements"] = list(refinements or [])

            with observe("memory", "persist_day", date) as rec:
                self.memory.persist_day(day, writeup)
                rec["output"] = f"persisted {date}"

            if move_processed:
                for b in batches:
                    capture.mark_processed(b)

            day_results.append({
                "date": date,
                "writeup": writeup,
                "day": day,
                "num_blocks": len(day["blocks"]),
                "num_events": len(by_date[date]),
            })

        latest = day_results[-1]
        return {**latest, "days": day_results}

    def _merge_with_stored(self, date: str, day: dict) -> dict:
        """Fold previously-stored pipeline blocks for ``date`` into a fresh day summary.

        A day is often processed more than once ("what did I do" at noon and again at night):
        the noon run already moved its events out of ``pending/``, so the night run only sees
        the afternoon — persisting it as-is would erase the morning. Stored derived blocks
        whose events aren't in this run are therefore kept and re-aggregated with the new ones.

        Stored blocks that share any source event with the new result are dropped in favour of
        the fresh analysis (that's a crash-retry: events analyzed and persisted, but the files
        never made it out of pending). Manual (user-authored) blocks are untouched — they're
        preserved by the store itself on save.
        """
        existing = [b for b in self.memory.get_blocks(date) if not b.get("manual")]
        if not existing:
            return day
        new_ids = {eid for blk in day["blocks"] for eid in blk["source_event_ids"]}
        kept = [b for b in existing if not (set(b["source_event_ids"]) & new_ids)]
        if not kept:
            return day
        return summarize_day(date, kept + day["blocks"])
