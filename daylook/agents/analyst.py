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

from daylook import cache, config
from daylook.events import CaptureEvent, EventBatch
from daylook.imaging import prepare_image

# Bump when the prompt/schema changes so stale cache entries are naturally bypassed.
PROMPT_VERSION = "analyst-v5"  # v5: content-aware productivity (social/entertainment) (v4 titles)

CATEGORIES = [
    "coding", "research", "communication", "review", "meeting", "writing",
    "planning", "break", "distraction", "social", "entertainment", "other",
]


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


# --- Keyframe selection (deterministic cost control) ----------------------------------

def select_keyframes(batch: EventBatch, max_images: int | None = None) -> set[str]:
    """Pick which events get their screenshot sent to the model.

    Cost discipline for heartbeat-dense batches: at 12s cadence an hour of stable work is
    ~300 events, and sending every screenshot would burn ~25x the tokens for no new
    information (the screen barely changes between heartbeats). So: every SWITCH event is a
    keyframe (the screen genuinely changed), and remaining slots are filled with heartbeats
    sampled evenly across the batch — periodic peeks at how the stable session is evolving.
    Non-keyframe events still reach the model as text metadata and still land in blocks'
    ``source_event_ids``; they just don't carry an image.
    """
    cap = max_images if max_images is not None else config.MAX_IMAGES_PER_BATCH
    switches = [e.event_id for e in batch.events if e.kind != "heartbeat"]
    keyframes = set(switches[:cap])

    slots = cap - len(keyframes)
    heartbeats = [e.event_id for e in batch.events if e.kind == "heartbeat"]
    if slots > 0 and heartbeats:
        stride = max(1, len(heartbeats) // slots)
        keyframes.update(heartbeats[::stride][:slots])
    return keyframes


# --- LLM grouping (vision, cached) ----------------------------------------------------

def _cache_key(batch: EventBatch, images: dict[str, bytes]) -> str:
    meta = [
        {"id": e.event_id, "t": e.timestamp, "app": e.app_name, "win": e.window_title,
         "kind": e.kind}
        for e in batch.events
    ]
    return cache.make_key(
        PROMPT_VERSION, config.DEFAULT_MODEL, meta, *(images[k] for k in sorted(images))
    )


_INSTRUCTION = (
    "You are the Analyst in a personal work-journal system. You receive a batch of "
    "screen-capture events in chronological order. Each event has an app name, a window "
    "title, a timestamp, and a screenshot of what was on screen.\n\n"
    "Group CONSECUTIVE events that belong to the same real activity/context into blocks. A "
    "block is one coherent thing the person was doing.\n\n"
    "Think in terms of a readable daily journal, not a minute-by-minute log. Prefer FEWER, "
    "LARGER blocks: a stretch of related work is ONE block even if the person dipped between a "
    "couple of apps to do it. Only start a new block at a genuine change of what they were "
    "actually doing, not at every window switch.\n\n"
    "Rules:\n"
    "- Only group consecutive events. Never reorder.\n"
    "- Every event must belong to exactly one block. Reference events by their exact event_id.\n"
    "- Write each label as a short, specific DESCRIPTION of what the person was doing, like a "
    "calendar-event title — roughly 4 to 10 words. Name the actual task, using what the "
    "screenshots show, not just the app. Good: 'Debugging the login redirect loop', "
    "'Researching JWT rotation on MDN and Stack Overflow', 'Watching a studio-apartment design "
    "video', 'Replying to the team standup thread'. Bad: just an app name ('Chrome'), one bare "
    "word ('Coding'), or a whole paragraph. Never mention this journal, the tracking, or the "
    "app you're labelling for.\n"
    "- Use the screenshots to make the label specific about what was actually happening, not "
    "just the app name.\n"
    "- Events marked kind=heartbeat mean the person stayed in the SAME window (captured "
    "periodically during stable work). Runs of heartbeats almost always continue the current "
    "activity; only screenshots are proof of a content change within them. Most heartbeats "
    "have no screenshot attached — that's normal, group them by their app/window metadata.\n"
    "- Judge productivity by CONTENT, not by the app. The SAME app can be productive or not: "
    "reading docs / a tutorial / researching a problem on YouTube or the web is 'research'; "
    "aimless feed-scrolling, entertainment videos, or blind-dating clips on the same YouTube is "
    "'social' or 'entertainment'. Use the screenshots to tell which it was. Twitter/X, "
    "Instagram, TikTok, Reddit browsing default to 'social' unless the screen shows genuine "
    "work/research.\n"
    f"- category must be one of: {', '.join(CATEGORIES)}. Guide: coding/writing/review/"
    "planning/meeting = focused work; research = learning with intent; communication = "
    "chat/email; social = feeds & messaging-for-fun; entertainment = video/music for leisure; "
    "distraction = clearly off-task; break = away/idle.\n"
)


def _build_content(batch: EventBatch, images: dict[str, bytes]):
    from google.genai import types

    parts = [types.Part(text=_INSTRUCTION)]
    valid = ", ".join(e.event_id for e in batch.events)
    parts.append(types.Part(text=f"\nBatch {batch.batch_id} — valid event_ids: {valid}\n"))
    for e in batch.events:
        img = images.get(e.event_id)
        parts.append(types.Part(text=(
            f"\nEvent {e.event_id} | {e.dt.strftime('%H:%M:%S')} | kind={e.kind} | "
            f"app={e.app_name} | window='{e.window_title}'"
            + (" | screenshot:" if img is not None else "")
        )))
        if img is not None:
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


async def _run_llm_async(batch: EventBatch, images: dict[str, bytes]) -> _Grouping:
    from google.adk.runners import InMemoryRunner

    agent = _make_agent()
    runner = InMemoryRunner(agent=agent, app_name="daylook-analyst")
    session = await runner.session_service.create_session(
        app_name="daylook-analyst", user_id="analyst"
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
    """The (cached) vision call that groups a batch. Prepares/downscales keyframe images only."""
    src = Path(pending_dir) if pending_dir is not None else config.pending_dir()
    keyframes = select_keyframes(batch)
    images = {
        e.event_id: prepare_image(e.image_path(src))
        for e in batch.events
        if e.event_id in keyframes and e.image_path(src).exists()
    }

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


# --- Block merging (deterministic de-fragmentation for a readable journal) -------------

def _merge_two(a: dict, b: dict) -> dict:
    """Fuse block ``b`` into ``a`` (a precedes b). Keeps the longer block's identity so a tiny
    blip absorbed into a big session takes the session's label/app, not the other way round.
    Times and source ids are recomputed from the real data, so the anti-hallucination invariant
    (source_event_ids ⊆ captured events) survives the merge."""
    lead = a if a["duration_seconds"] >= b["duration_seconds"] else b
    return {
        "start": a["start"],
        "end": b["end"],
        "duration_seconds": a["duration_seconds"] + b["duration_seconds"],
        "label": lead["label"],
        "category": lead["category"],
        "app_name": lead["app_name"],
        "source_event_ids": list(a["source_event_ids"]) + list(b["source_event_ids"]),
    }


def merge_blocks(blocks: list[dict], min_seconds: int | None = None) -> list[dict]:
    """Collapse a fragmented block list into a few meaningful ones — the difference between a
    minute-by-minute log and a journal. Two passes, repeated until stable:

      1. Adjacent blocks with the SAME app and category are one activity — merge them.
      2. Any block still shorter than ``min_seconds`` is noise on its own — fold it into its
         larger neighbour (a 40s Safari glance mid-Terminal session joins the session).

    Purely a presentation transform over already-derived blocks; it never invents data and every
    surviving block still lists the real event ids it came from.
    """
    cutoff = min_seconds if min_seconds is not None else config.MIN_BLOCK_SECONDS
    blocks = [dict(b) for b in blocks]

    def one_pass(bs: list[dict]) -> tuple[list[dict], bool]:
        out: list[dict] = []
        changed = False
        for b in bs:
            if out and out[-1]["app_name"] == b["app_name"] and out[-1]["category"] == b["category"]:
                out[-1] = _merge_two(out[-1], b)
                changed = True
            else:
                out.append(b)
        return out, changed

    # Pass 1: repeatedly merge same app+category neighbours.
    changed = True
    while changed and len(blocks) > 1:
        blocks, changed = one_pass(blocks)

    # Pass 2: fold sub-cutoff blocks into whichever neighbour is longer, then re-merge.
    changed = True
    while changed and len(blocks) > 1:
        changed = False
        for i, b in enumerate(blocks):
            if b["duration_seconds"] >= cutoff:
                continue
            prev, nxt = (blocks[i - 1] if i > 0 else None), (blocks[i + 1] if i + 1 < len(blocks) else None)
            if prev is None and nxt is None:
                continue  # a lone short day: leave it, it's all we have
            # Fold into the longer neighbour so the dominant activity keeps its identity.
            into_prev = prev is not None and (nxt is None or prev["duration_seconds"] >= nxt["duration_seconds"])
            if into_prev:
                blocks[i - 1] = _merge_two(prev, b)
            else:
                blocks[i + 1] = _merge_two(b, nxt)
            del blocks[i]
            changed = True
            break
        if not changed:
            break
        blocks, _ = one_pass(blocks)
    return blocks


# --- Day-level aggregation ------------------------------------------------------------

# The keys a day-summary block carries. Blocks read back from the store also carry db-only
# columns (id, date, manual); summarize_day strips to this shape so stored and fresh blocks
# can be combined without leaking storage details into the pipeline.
_BLOCK_KEYS = ("start", "end", "duration_seconds", "label", "category", "app_name",
               "source_event_ids")


def summarize_day(date: str | None, blocks: list[dict]) -> dict:
    """Build the canonical day-summary dict from a flat block list.

    De-fragments the blocks (see :func:`merge_blocks`) and recomputes every aggregate from
    the surviving blocks, so callers can hand in any mix of freshly-derived and
    previously-stored blocks (e.g. when a second pipeline run the same day merges with the
    morning's result) and the totals always match what's shown."""
    clean = [{k: b[k] for k in _BLOCK_KEYS} for b in sorted(blocks, key=lambda b: b["start"])]
    merged = merge_blocks(clean)
    by_cat: Counter[str] = Counter()
    by_app: Counter[str] = Counter()
    for b in merged:
        by_cat[b["category"]] += b["duration_seconds"]
        by_app[b["app_name"]] += b["duration_seconds"]
    total = sum(b["duration_seconds"] for b in merged)
    return {
        "date": date,
        "blocks": merged,
        "total_tracked_seconds": total,
        "time_by_category": dict(by_cat),
        "time_by_app": dict(by_app),
        "app_ranking": [app for app, _ in by_app.most_common()],
    }


def aggregate_day(analyses: list[BatchAnalysis]) -> dict:
    """Combine batch analyses into a day summary: all blocks + time distributions + ranking."""
    raw = [b.to_dict() for a in analyses for b in a.blocks]
    return summarize_day(analyses[0].date if analyses else None, raw)
