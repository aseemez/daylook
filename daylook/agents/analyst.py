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
PROMPT_VERSION = "analyst-v6"  # v6: per-block `detail` sentences (v5 content-aware categories)

CATEGORIES = [
    "coding", "research", "communication", "review", "meeting", "writing",
    "planning", "break", "distraction", "social", "entertainment", "other",
]


# --- LLM output schema (grouping only — no times, no apps) ----------------------------

class _Group(BaseModel):
    label: str
    category: str
    # 1–2 sentences of concrete specifics for the block (what exactly, from the screenshots).
    # Optional so an old cached grouping (pre-v6 shape) still validates; empty means "no
    # extra detail beyond the label".
    detail: str = ""
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
    # 1–2 concrete sentences of what actually happened (may be ""). Last + defaulted so
    # pre-detail call sites and fixtures construct unchanged.
    detail: str = ""

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


# --- Detail hygiene (detailed, but never verbose or repetitive) -----------------------

def tidy_detail(text: str, max_sentences: int = 2, max_chars: int = 260) -> str:
    """Keep a detail specific but tight: the user asked for detailed, NOT verbose or vague.

    Merging blocks concatenates their details, and the model sometimes restates the same fact
    a few ways — so a merged 'Away' stretch could end up 4 near-identical sentences. This
    splits into sentences, drops near-duplicates (case-insensitive, ignoring leading filler),
    keeps at most ``max_sentences`` whole sentences that fit ``max_chars``, and only ever cuts
    mid-sentence as a last resort. **Idempotent** — running it on its own output is a no-op, so
    it's safe to call at derivation and again at every merge without piling up ellipses. Purely
    cosmetic: no facts are invented, only trimmed."""
    if not text:
        return ""
    import re
    # Normalise leftover truncation marks from a previous pass (…, trailing dashes, dot runs)
    # so re-tidying doesn't compound them.
    text = re.sub(r"[…]+", " ", text)
    text = re.sub(r"\.{2,}", " ", text)
    text = re.sub(r"\s*[—\-]\s*$", "", text.strip())
    raw = re.split(r"(?<=[.!?])\s+", text.strip())
    seen: set[str] = set()
    kept: list[str] = []
    total = 0
    for s in raw:
        s = s.strip().strip("—- ")
        if not s:
            continue
        norm = re.sub(r"[^a-z0-9 ]", "", s.lower())
        norm = re.sub(r"^(the|a|then|also|and|screen|user|it|this) ", "", norm).strip()
        key = norm[:50]
        if key in seen:
            continue
        seen.add(key)
        # Take a whole sentence if it fits; stop once we'd blow the budget (but always keep 1).
        if kept and total + len(s) + 1 > max_chars:
            break
        kept.append(s)
        total += len(s) + 1
        if len(kept) >= max_sentences:
            break
    out = " ".join(kept)
    if len(out) > max_chars:  # a single over-long sentence: hard cut, one ellipsis
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:.—- ") + "…"
    return out


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
            detail = (grouping.blocks[cur_key].detail or "").strip()
        else:
            label, category, detail = "Unlabeled activity", "other", ""
        detail = tidy_detail(detail)
        if category not in CATEGORIES:
            category = "other"
        app_name = Counter(e.app_name for e in cur).most_common(1)[0][0]
        blocks.append(LabeledBlock(
            start=start_dt.isoformat(timespec="seconds"),
            end=end_dt.isoformat(timespec="seconds"),
            duration_seconds=total,
            label=label,
            category=category,
            detail=detail,
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


# --- Screen-change evidence (deterministic common sense) ------------------------------

def annotate_screen_changes(batch: EventBatch, pending_dir: Path) -> dict[str, bool]:
    """event_id -> whether its screenshot visibly differs from the previous event's.

    The plausibility signal the model can't fake and can't miss: real watching/reading/
    working always moves pixels over minutes (video frames advance, pages scroll, text
    appears), so a long run of unchanged frames is proof of an empty chair — a 10-minute
    video "watched" for 92 minutes was really a paused player nobody was in front of.
    Every capture event has its screenshot on disk (keyframe selection only limits what's
    *sent* to the model), so this is computed locally for all of them and ridden into the
    prompt as text. Uses the capture layer's tolerance diff (menu-bar crop, caret-proof).
    Missing/unreadable images count as 'changed' — uncertainty must never fabricate idleness.
    """
    from PIL import Image

    from daylook.capture_script.real_capture import frame_signature, frames_differ

    changed: dict[str, bool] = {}
    prev: bytes | None = None
    for e in batch.events:
        path = e.image_path(pending_dir)
        try:
            with Image.open(path) as im:
                sig = frame_signature(im.convert("RGB"))
        except Exception:
            changed[e.event_id] = True
            continue
        changed[e.event_id] = frames_differ(prev, sig)
        prev = sig
    return changed


# --- LLM grouping (vision, cached) ----------------------------------------------------

def _cache_key(batch: EventBatch, images: dict[str, bytes], changes: dict[str, bool]) -> str:
    meta = [
        {"id": e.event_id, "t": e.timestamp, "app": e.app_name, "win": e.window_title,
         "kind": e.kind, "chg": changes.get(e.event_id, True)}
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
    "- For each block also write 'detail': 1–2 sentences of CONCRETE specifics pulled from "
    "the screenshots and window titles — the exact video/article/doc titles, channel or site "
    "names, repo/file names, people, error messages, search queries, what changed between the "
    "start and end of the block. Write it like a note to the person about what they actually "
    "did, e.g. 'Watched \"AI coding workflows\" by Matt Pocock, mostly the section on cursor "
    "rules; left it paused around the 40-min mark.' NEVER vague filler ('browsed the web', "
    "'worked in the app', 'spent time on various tasks') and never padding — if the "
    "screenshots show nothing beyond the label, keep detail short or empty rather than "
    "inventing. Facts only, no commentary on productivity.\n"
    "- Events marked kind=heartbeat mean the person stayed in the SAME window (captured "
    "periodically during stable work). Runs of heartbeats almost always continue the current "
    "activity; only screenshots are proof of a content change within them. Most heartbeats "
    "have no screenshot attached — that's normal, group them by their app/window metadata.\n"
    "- SANITY-CHECK long stretches against the evidence of change. Every event is annotated "
    "screen=changed or screen=static (whether its screenshot visibly differs from the "
    "previous one). Genuinely watching, reading, or working ALWAYS changes the screen over "
    "minutes — video frames advance, pages scroll, text gets typed. A long run of "
    "screen=static events means the person was NOT there: a paused video or an abandoned "
    "page was simply left open. Label such stretches category 'break' with an honest label "
    "(e.g. 'Away — YouTube video left paused'), no matter what app was on screen, and never "
    "credit an unchanged screen as continuous activity.\n"
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


def _build_content(batch: EventBatch, images: dict[str, bytes],
                   changes: dict[str, bool] | None = None):
    from google.genai import types

    changes = changes or {}
    parts = [types.Part(text=_INSTRUCTION)]
    valid = ", ".join(e.event_id for e in batch.events)
    parts.append(types.Part(text=f"\nBatch {batch.batch_id} — valid event_ids: {valid}\n"))
    for e in batch.events:
        img = images.get(e.event_id)
        screen = "changed" if changes.get(e.event_id, True) else "static"
        parts.append(types.Part(text=(
            f"\nEvent {e.event_id} | {e.dt.strftime('%H:%M:%S')} | kind={e.kind} | "
            f"app={e.app_name} | window='{e.window_title}' | screen={screen}"
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


async def _run_llm_async(batch: EventBatch, images: dict[str, bytes],
                         changes: dict[str, bool] | None = None) -> _Grouping:
    from google.adk.runners import InMemoryRunner

    agent = _make_agent()
    runner = InMemoryRunner(agent=agent, app_name="daylook-analyst")
    session = await runner.session_service.create_session(
        app_name="daylook-analyst", user_id="analyst"
    )
    content = _build_content(batch, images, changes)
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
    changes: dict[str, bool] | None = None,
) -> _Grouping:
    """The (cached) vision call that groups a batch. Prepares/downscales keyframe images only.

    ``changes`` is the screen-change annotation map; pass it when already computed (the
    pipeline does — each event's image is opened once per run, which matters for a 10-hour
    heartbeat-dense day), or leave None to compute here."""
    src = Path(pending_dir) if pending_dir is not None else config.pending_dir()
    keyframes = select_keyframes(batch)
    images = {
        e.event_id: prepare_image(e.image_path(src))
        for e in batch.events
        if e.event_id in keyframes and e.image_path(src).exists()
    }
    if changes is None:
        changes = annotate_screen_changes(batch, src)

    key = _cache_key(batch, images, changes)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return _Grouping(**hit)

    # Bounded like every other model call (see adk_utils.CALL_TIMEOUT_SECONDS): a vision
    # call that stalls raises here — leaving the batch's events pending for a later retry —
    # instead of wedging the pipeline for hours.
    from daylook.adk_utils import CALL_TIMEOUT_SECONDS
    try:
        grouping = asyncio.run(
            asyncio.wait_for(_run_llm_async(batch, images, changes),
                             timeout=CALL_TIMEOUT_SECONDS)
        )
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"analyst vision call for batch {batch.batch_id} did not answer "
            f"within {CALL_TIMEOUT_SECONDS}s"
        ) from None
    cache.set(key, grouping.model_dump())
    return grouping


# --- Deterministic away-time enforcement ----------------------------------------------

def enforce_static_breaks(
    analysis: BatchAnalysis,
    events: list[CaptureEvent],
    changes: dict[str, bool],
    all_events: list[CaptureEvent] | None = None,
    threshold_seconds: float | None = None,
) -> BatchAnalysis:
    """Carve provably-idle stretches out of active blocks — in code, not by prompt.

    The screen-change annotations are *evidence*; this is the *rule*: a run of consecutive
    events whose frames never visibly changed, lasting at least ``STALE_PAUSE_SECONDS``
    (the same 10-minute bar the live capture loop uses before auto-pausing), cannot be
    active work — nobody watches a video or reads a page for 10+ minutes without a single
    pixel moving. Such runs become ``break`` blocks ("Away — <app> left open") regardless
    of how the model grouped or labeled them, so a paused video can never again be credited
    as a 92-minute session. Shorter static stretches are left to the model's judgment (a
    slow reader on one page is legitimate; that ambiguity is why the bar matches capture's).
    """
    thr = (threshold_seconds if threshold_seconds is not None
           else float(config.STALE_PAUSE_SECONDS))
    if thr <= 0 or not analysis.blocks:
        return analysis
    dwell = compute_dwell(all_events if all_events is not None else list(events))
    by_id = {e.event_id: e for e in events}

    out: list[LabeledBlock] = []
    for blk in analysis.blocks:
        evs = [by_id[eid] for eid in blk.source_event_ids if eid in by_id]
        if not evs:
            out.append(blk)
            continue
        # Split the block's events into alternating active / long-static segments.
        segments: list[tuple[bool, list[CaptureEvent]]] = []  # (is_away, events)
        run: list[CaptureEvent] = []

        def close_run() -> None:
            if not run:
                return
            away = (all(not changes.get(e.event_id, True) for e in run)
                    and sum(dwell.get(e.event_id, 0) for e in run) >= thr)
            if segments and segments[-1][0] == away:
                segments[-1][1].extend(run)
            else:
                segments.append((away, list(run)))

        for e in evs:
            static = not changes.get(e.event_id, True)
            if run and static != (not changes.get(run[-1].event_id, True)):
                close_run()
                run = []
            run.append(e)
        close_run()
        # Merge any static run that was too short to count back into its neighbours.
        merged: list[tuple[bool, list[CaptureEvent]]] = []
        for away, seg in segments:
            if merged and merged[-1][0] == away:
                merged[-1][1].extend(seg)
            else:
                merged.append((away, list(seg)))

        if not any(away for away, _ in merged):
            out.append(blk)
            continue
        for away, seg in merged:
            start_dt = seg[0].dt
            total = sum(dwell.get(e.event_id, 0) for e in seg)
            end_dt = start_dt + timedelta(seconds=total)
            app = Counter(e.app_name for e in seg).most_common(1)[0][0]
            if away:
                out.append(LabeledBlock(
                    start=start_dt.isoformat(timespec="seconds"),
                    end=end_dt.isoformat(timespec="seconds"),
                    duration_seconds=total,
                    label=f"Away — {app} left open",
                    category="break",
                    app_name=app,
                    source_event_ids=[e.event_id for e in seg],
                    detail="Screen didn't change the whole time — likely away from the computer.",
                ))
            else:
                out.append(LabeledBlock(
                    start=start_dt.isoformat(timespec="seconds"),
                    end=end_dt.isoformat(timespec="seconds"),
                    duration_seconds=total,
                    label=blk.label,
                    category=blk.category,
                    app_name=app,
                    source_event_ids=[e.event_id for e in seg],
                    detail=blk.detail,
                ))
    return BatchAnalysis(batch_id=analysis.batch_id, date=analysis.date, blocks=out)


# --- Public entry points --------------------------------------------------------------

def analyze_batch(
    batch: EventBatch,
    pending_dir: Path | None = None,
    all_events: list[CaptureEvent] | None = None,
    use_cache: bool = True,
) -> BatchAnalysis:
    """Analyze one batch end-to-end: LLM grouping (cached) -> deterministic derivation,
    then the deterministic away-time rule (see :func:`enforce_static_breaks`)."""
    src = Path(pending_dir) if pending_dir is not None else config.pending_dir()
    changes = annotate_screen_changes(batch, src)
    grouping = llm_grouping(batch, pending_dir=pending_dir, use_cache=use_cache,
                            changes=changes)
    analysis = derive_blocks(batch, grouping, all_events=all_events)
    return enforce_static_breaks(analysis, list(batch.events), changes,
                                 all_events=all_events)


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
    # Details are the journal's substance — merging must not throw the absorbed block's away.
    # Concatenate in chronological order, dedupe, and cap so a many-way merge stays a note,
    # not an essay.
    details = [d for d in dict.fromkeys(
        [(a.get("detail") or "").strip(), (b.get("detail") or "").strip()]) if d]
    return {
        "start": a["start"],
        "end": b["end"],
        "duration_seconds": a["duration_seconds"] + b["duration_seconds"],
        "label": lead["label"],
        "category": lead["category"],
        "detail": tidy_detail(" ".join(details)),
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
_BLOCK_KEYS = ("start", "end", "duration_seconds", "label", "category", "detail", "app_name",
               "source_event_ids")


def summarize_day(date: str | None, blocks: list[dict]) -> dict:
    """Build the canonical day-summary dict from a flat block list.

    De-fragments the blocks (see :func:`merge_blocks`) and recomputes every aggregate from
    the surviving blocks, so callers can hand in any mix of freshly-derived and
    previously-stored blocks (e.g. when a second pipeline run the same day merges with the
    morning's result) and the totals always match what's shown."""
    # .get for `detail`: older stored rows and hand-written fixtures predate the column.
    clean = [{k: (b[k] if k != "detail" else (b.get("detail") or "")) for k in _BLOCK_KEYS}
             for b in sorted(blocks, key=lambda b: b["start"])]
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
