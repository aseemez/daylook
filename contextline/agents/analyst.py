"""Analyst Agent (build step 3) — the reasoning core.

Takes a batch of capture events (each with a screenshot) and produces labeled *semantic
blocks*: contiguous runs of same-context activity with a human label, a category, and the
exact source events that make them up.

Design that makes the anti-hallucination guarantee hold by construction:

  * The **LLM only groups and labels** — it decides which consecutive events belong to the
    same activity and what to call it. It never invents times or apps.
  * **Deterministic code derives everything factual** — a block's start/end/duration come
    from the real event timestamps, its ``app_name`` from the real events, and its
    ``source_event_ids`` are validated to be a subset of the batch. Every block therefore
    provably traces back to captured data (what ``eval.py`` checks).

This is the only agent that receives images. Vision calls are cached on-disk (keyed on the
exact input) so reruns are free.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from contextline import cache, config
from contextline.events import CaptureEvent, EventBatch
from contextline.imaging import prepare_image

# Bump when the prompt/schema changes so stale cache entries are naturally bypassed.
PROMPT_VERSION = "analyst-v1"

CATEGORIES = ["coding", "research", "communication", "review", "meeting", "writing", "break", "other"]


# --- LLM output schema (grouping only — no times, no apps) ----------------------------

class _Group(BaseModel):
    label: str
    category: str
    event_ids: list[str]


class _Grouping(BaseModel):
    blocks: list[_Group]


# --- Analyst output types -------------------------------------------------------------

@dataclass(frozen=True)
class LabeledBlock:
    start: str            # ISO 8601
    end: str              # ISO 8601
    duration_seconds: int
    label: str
    category: str
    app_name: str
    source_event_ids: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BatchAnalysis:
    batch_id: str
    date: str
    blocks: list[LabeledBlock]

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "date": self.date,
            "blocks": [b.to_dict() for b in self.blocks],
            "time_by_category": self.time_by_category(),
            "time_by_app": self.time_by_app(),
        }

    def time_by_category(self) -> dict[str, int]:
        agg: Counter[str] = Counter()
        for b in self.blocks:
            agg[b.category] += b.duration_seconds
        return dict(agg)

    def time_by_app(self) -> dict[str, int]:
        agg: Counter[str] = Counter()
        for b in self.blocks:
            agg[b.app_name] += b.duration_seconds
        return dict(agg)


# --- Dwell / duration (deterministic) -------------------------------------------------

def compute_dwell(events: list[CaptureEvent]) -> dict[str, int]:
    """Seconds attributable to each event = gap to the next event, capped.

    Capping at ``MAX_EVENT_DWELL_SECONDS`` keeps idle gaps (and the final event of the day,
    which has no successor) from inflating a block. Pass the *full day's* events so dwells at
    batch boundaries are correct.
    """
    ordered = sorted(events, key=lambda e: (e.timestamp, e.event_id))
    cap = config.MAX_EVENT_DWELL_SECONDS
    dwell: dict[str, int] = {}
    gaps: list[int] = []
    for a, b in zip(ordered, ordered[1:]):
        capped = min(int((b.dt - a.dt).total_seconds()), cap)
        dwell[a.event_id] = capped
        gaps.append(capped)
    if ordered:
        # last event: no successor — use the median observed dwell (fallback 120s), capped.
        last_default = int(statistics.median(gaps)) if gaps else 120
        dwell[ordered[-1].event_id] = min(last_default, cap)
    return dwell


# --- Block derivation (deterministic, anti-hallucination) -----------------------------

def derive_blocks(
    batch: EventBatch,
    grouping: _Grouping,
    all_events: list[CaptureEvent] | None = None,
) -> BatchAnalysis:
    """Turn the LLM's grouping into validated LabeledBlocks with real times and apps.

    Guarantees regardless of what the LLM returned:
      * every event in the batch appears in exactly one block (full coverage),
      * blocks contain only consecutive events (no reordering),
      * every ``source_event_id`` is a real event id from the batch,
      * start/end/duration and app_name are derived from real event data, never the model.
    """
    events = list(batch.events)  # already chronological
    valid_ids = {e.event_id for e in events}
    dwell = compute_dwell(all_events if all_events is not None else events)

    # event_id -> index of its LLM group (first assignment wins; unknown ids ignored)
    assign: dict[str, int] = {}
    for gi, g in enumerate(grouping.blocks):
        for eid in g.event_ids:
            if eid in valid_ids and eid not in assign:
                assign[eid] = gi

    # Walk events in order. A new block starts when the group key changes. Unassigned events
    # continue the current block (or, if first, open a fallback block) — nothing is dropped.
    FALLBACK = -1
    blocks: list[LabeledBlock] = []
    cur: list[CaptureEvent] = []
    cur_key: int | None = None

    def flush() -> None:
        if not cur:
            return
        start_dt = cur[0].dt
        total = sum(dwell.get(e.event_id, 0) for e in cur)
        end_dt = start_dt + timedelta(seconds=total)
        if cur_key is not None and cur_key >= 0 and cur_key < len(grouping.blocks):
            label = grouping.blocks[cur_key].label
            category = grouping.blocks[cur_key].category
        else:
            label, category = "Unlabeled activity", "other"
        if category not in CATEGORIES:
            category = "other"
        app_name = Counter(e.app_name for e in cur).most_common(1)[0][0]
        blocks.append(LabeledBlock(
            start=start_dt.isoformat(timespec="seconds"),
            end=end_dt.isoformat(timespec="seconds"),
            duration_seconds=total,
            label=label,
            category=category,
            app_name=app_name,
            source_event_ids=[e.event_id for e in cur],
        ))

    prev_key: int | None = None
    for e in events:
        key = assign.get(e.event_id, None)
        if key is None:
            key = prev_key if cur else FALLBACK  # inherit current block, else fallback
        if cur and key != prev_key:
            flush()
            cur = []
        cur.append(e)
        cur_key = key
        prev_key = key
    flush()

    return BatchAnalysis(batch_id=batch.batch_id, date=batch.date, blocks=blocks)


# --- LLM grouping (vision, cached) ----------------------------------------------------

def _cache_key(batch: EventBatch, images: list[bytes]) -> str:
    meta = [
        {"id": e.event_id, "t": e.timestamp, "app": e.app_name, "win": e.window_title}
        for e in batch.events
    ]
    return cache.make_key(PROMPT_VERSION, config.DEFAULT_MODEL, meta, *images)


_INSTRUCTION = (
    "You are the Analyst in a personal work-journal system. You receive a batch of "
    "screen-capture events in chronological order. Each event has an app name, a window "
    "title, a timestamp, and a screenshot of what was on screen.\n\n"
    "Group CONSECUTIVE events that belong to the same real activity/context into blocks. A "
    "block is one coherent thing the person was doing, e.g. 'Debugging JWT token rotation in "
    "Cursor' or 'Researching key rotation on Stack Overflow and MDN'.\n\n"
    "Rules:\n"
    "- Only group consecutive events. Never reorder.\n"
    "- Every event must belong to exactly one block. Reference events by their exact event_id.\n"
    "- Start a new block at a genuine context switch; otherwise keep related events together.\n"
    "- Use the screenshots to make the label specific about what was actually happening, not "
    "just the app name.\n"
    f"- category must be one of: {', '.join(CATEGORIES)}.\n"
)


def _build_content(batch: EventBatch, images: list[bytes]):
    from google.genai import types

    parts = [types.Part(text=_INSTRUCTION)]
    valid = ", ".join(e.event_id for e in batch.events)
    parts.append(types.Part(text=f"\nBatch {batch.batch_id} — valid event_ids: {valid}\n"))
    for e, img in zip(batch.events, images):
        parts.append(types.Part(text=(
            f"\nEvent {e.event_id} | {e.dt.strftime('%H:%M:%S')} | app={e.app_name} | "
            f"window='{e.window_title}' | screenshot:"
        )))
        parts.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))
    return types.Content(role="user", parts=parts)


def _make_agent():
    from google.adk.agents import LlmAgent
    return LlmAgent(
        name="analyst",
        model=config.DEFAULT_MODEL,
        instruction=_INSTRUCTION,
        output_schema=_Grouping,
    )


async def _run_llm_async(batch: EventBatch, images: list[bytes]) -> _Grouping:
    from google.adk.runners import InMemoryRunner

    agent = _make_agent()
    runner = InMemoryRunner(agent=agent, app_name="contextline-analyst")
    session = await runner.session_service.create_session(
        app_name="contextline-analyst", user_id="analyst"
    )
    content = _build_content(batch, images)
    text = None
    async for ev in runner.run_async(
        user_id="analyst", session_id=session.id, new_message=content
    ):
        if ev.is_final_response() and ev.content and ev.content.parts:
            text = ev.content.parts[0].text
    if not text:
        return _Grouping(blocks=[])
    return _Grouping(**json.loads(text))


def llm_grouping(
    batch: EventBatch,
    pending_dir: Path | None = None,
    use_cache: bool = True,
) -> _Grouping:
    """The (cached) vision call that groups a batch. Prepares/downscales images first."""
    src = Path(pending_dir) if pending_dir is not None else config.pending_dir()
    images = [prepare_image(e.image_path(src)) for e in batch.events]

    key = _cache_key(batch, images)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return _Grouping(**hit)

    grouping = asyncio.run(_run_llm_async(batch, images))
    cache.set(key, grouping.model_dump())
    return grouping


# --- Public entry points --------------------------------------------------------------

def analyze_batch(
    batch: EventBatch,
    pending_dir: Path | None = None,
    all_events: list[CaptureEvent] | None = None,
    use_cache: bool = True,
) -> BatchAnalysis:
    """Analyze one batch end-to-end: LLM grouping (cached) -> deterministic derivation."""
    grouping = llm_grouping(batch, pending_dir=pending_dir, use_cache=use_cache)
    return derive_blocks(batch, grouping, all_events=all_events)


def analyze_batches(
    batches: list[EventBatch],
    pending_dir: Path | None = None,
    use_cache: bool = True,
) -> list[BatchAnalysis]:
    """Analyze several batches. Dwells are computed across all events for correct boundaries."""
    all_events: list[CaptureEvent] = [e for b in batches for e in b.events]
    return [
        analyze_batch(b, pending_dir=pending_dir, all_events=all_events, use_cache=use_cache)
        for b in batches
    ]


# --- Day-level aggregation ------------------------------------------------------------

def aggregate_day(analyses: list[BatchAnalysis]) -> dict:
    """Combine batch analyses into a day summary: all blocks + time distributions + ranking."""
    blocks = [b for a in analyses for b in a.blocks]
    by_cat: Counter[str] = Counter()
    by_app: Counter[str] = Counter()
    for b in blocks:
        by_cat[b.category] += b.duration_seconds
        by_app[b.app_name] += b.duration_seconds
    total = sum(b.duration_seconds for b in blocks)
    return {
        "date": analyses[0].date if analyses else None,
        "blocks": [b.to_dict() for b in blocks],
        "total_tracked_seconds": total,
        "time_by_category": dict(by_cat),
        "time_by_app": dict(by_app),
        "app_ranking": [app for app, _ in by_app.most_common()],
    }
