"""The processing pipeline: capture -> analyst -> journalist -> memory.

One place that runs a day end-to-end, shared by the Root Agent, ``eval.py``, and the FastAPI
app so they can't drift. The per-agent model calls are injectable, which lets tests drive the
whole orchestration deterministically without touching Vertex.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from contextline.agents.analyst import aggregate_day, analyze_batches
from contextline.agents.capture import CaptureAgent
from contextline.agents.journalist import write_journal
from contextline.agents.memory import MemoryAgent


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
        """Ingest pending events, analyze, write the journal, persist. Returns a result dict.

        ``refinements`` (session-scoped user preferences) are threaded into the Journalist.
        ``move_processed`` moves ingested events to ``processed/`` after a successful persist;
        set False to keep a fixture reusable across runs (e.g. in eval).
        """
        capture = CaptureAgent(pending=pending_dir)
        batches = capture.batch_pending()
        if not batches:
            return {
                "date": None,
                "writeup": "No pending activity to process.",
                "day": {"date": None, "blocks": []},
                "num_blocks": 0,
                "num_events": 0,
            }

        analyses = self._analyze(batches, pending_dir=capture.pending, use_cache=use_cache)
        day = aggregate_day(analyses)
        writeup = self._journal(day, refinements=refinements, use_cache=use_cache)
        date = self.memory.persist_day(day, writeup)

        if move_processed:
            for b in batches:
                capture.mark_processed(b)

        return {
            "date": date,
            "writeup": writeup,
            "day": day,
            "num_blocks": len(day["blocks"]),
            "num_events": sum(len(b) for b in batches),
        }
