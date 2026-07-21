"""Capture Agent (build step 2).

Responsibility (and nothing more): read pending capture events, group them into batches
(logical sessions / bounded time windows), hand each batch to the Analyst, and move ingested
events out of ``pending/`` into ``processed/``. **No reasoning lives here** — the batching is
purely deterministic (time gaps and size caps), so it costs no model calls.

Read path.  The Capture Agent's only way to see files is through an ``EventSource``. The
*intended* implementation is an MCP filesystem server (``@modelcontextprotocol/server-filesystem``);
see ``daylook/agents/capture_mcp.py``. ``FilesystemEventSource`` below is the honest
plain-tool fallback that CLAUDE.md explicitly blesses so the pipeline is never blocked on MCP.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from daylook import config
from daylook.events import CaptureEvent, EventBatch, load_events


# --- Read path abstraction ------------------------------------------------------------

class EventSource(Protocol):
    """Where the Capture Agent gets pending events. MCP or plain filesystem behind this."""

    def list_events(self) -> list[CaptureEvent]:
        ...


class FilesystemEventSource:
    """Plain-tool fallback: read event sidecars straight off disk.

    This is the concrete stand-in for the MCP filesystem read path. It reads only the JSON
    sidecars (metadata); images are referenced by path and loaded later, and only by the
    Analyst, keeping every other layer text-only.
    """

    def __init__(self, pending: Path | None = None):
        self.pending = Path(pending) if pending is not None else config.pending_dir()

    def list_events(self) -> list[CaptureEvent]:
        if not self.pending.exists():
            return []
        return load_events(self.pending)


# --- Deterministic batching -----------------------------------------------------------

def batch_events(
    events: list[CaptureEvent],
    idle_gap_seconds: int | None = None,
    max_events: int | None = None,
    max_span_seconds: int | None = None,
    max_total_events: int | None = None,
) -> list[EventBatch]:
    """Split a time-sorted event list into batches. One batch -> one Analyst call.

    A new batch begins when any of these is true relative to the current batch:
      * the gap since the previous event is >= ``idle_gap_seconds`` (a real break), or
      * the current batch already holds ``max_events`` SWITCH events, or
      * the current batch holds ``max_total_events`` events of any kind, or
      * appending would stretch the batch past ``max_span_seconds`` of wall-clock.

    Only switch events count toward ``max_events``: heartbeats are dense (one per
    ~HEARTBEAT_SECONDS of stable work) but cheap — most travel as text, not images — so
    counting them against the vision cap would shatter a focus hour into dozens of calls.
    A switch-only stream (mocks, fixtures, pre-heartbeat data) batches exactly as it
    always did. All thresholds default to the values in ``config``. Deterministic.
    """
    idle_gap = idle_gap_seconds if idle_gap_seconds is not None else config.IDLE_GAP_SECONDS
    cap_n = max_events if max_events is not None else config.MAX_BATCH_EVENTS
    cap_span = max_span_seconds if max_span_seconds is not None else config.MAX_BATCH_SPAN_SECONDS
    cap_total = max_total_events if max_total_events is not None else config.MAX_BATCH_TOTAL_EVENTS

    if not events:
        return []

    ordered = sorted(events, key=lambda e: (e.timestamp, e.event_id))
    batches: list[EventBatch] = []
    current: list[CaptureEvent] = []
    current_switches = 0

    def flush() -> None:
        if not current:
            return
        start = current[0].dt
        batch_id = f"batch_{start.strftime('%Y%m%dT%H%M%S')}"
        batches.append(EventBatch(batch_id=batch_id, events=tuple(current)))

    for e in ordered:
        if current:
            gap = (e.dt - current[-1].dt).total_seconds()
            span = (e.dt - current[0].dt).total_seconds()
            if (
                gap >= idle_gap
                or (e.kind != "heartbeat" and current_switches >= cap_n)
                or len(current) >= cap_total
                or span >= cap_span
            ):
                flush()
                current = []
                current_switches = 0
        current.append(e)
        if e.kind != "heartbeat":
            current_switches += 1
    flush()

    return batches


# --- The agent ------------------------------------------------------------------------

class CaptureAgent:
    """Reads + batches pending events and moves ingested ones to ``processed/``.

    Kept as a plain component (not an LlmAgent) on purpose: there is no reasoning to do, so
    spending a model call here would be waste. The Root Agent drives it as a tool.
    """

    def __init__(
        self,
        pending: Path | None = None,
        processed: Path | None = None,
        source: EventSource | None = None,
    ):
        self.pending = Path(pending) if pending is not None else config.pending_dir()
        self.processed = Path(processed) if processed is not None else config.processed_dir()
        if source is not None:
            self.source = source
        else:
            # Lazy import avoids a capture <-> capture_mcp circular import. Prefers the MCP
            # read path (intended interface), falls back to plain filesystem if unavailable.
            from daylook.agents.capture_mcp import make_event_source
            self.source = make_event_source(self.pending)

    def read_pending(self) -> list[CaptureEvent]:
        return self.source.list_events()

    def batch_pending(self) -> list[EventBatch]:
        """Read pending events and group them into Analyst-sized batches. Moves nothing."""
        return batch_events(self.read_pending())

    def mark_processed(self, batch: EventBatch) -> list[str]:
        """Move a batch's event files (json + jpg) from pending to processed.

        Called only *after* a batch has been successfully handed off/analyzed, so a crash
        mid-pipeline leaves the events in pending to be retried rather than silently lost.
        Returns the event ids moved.
        """
        self.processed.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        for event in batch.events:
            for src in (event.sidecar_path(self.pending), event.image_path(self.pending)):
                if src.exists():
                    shutil.move(str(src), str(self.processed / src.name))
            moved.append(event.event_id)
        return moved

    def pending_count(self) -> int:
        return len(self.read_pending())
