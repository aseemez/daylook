"""Deterministic screen-change evidence for the Analyst (the "common sense" layer).

A 10-minute video can't be watched for 92 minutes: genuine activity moves pixels, so each
event is annotated with whether its screenshot differs from the previous one, and the
Analyst's prompt tells it to label long static runs as away-time. This pins the annotation
itself (pure image comparison, no model).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from daylook.agents.analyst import annotate_screen_changes
from daylook.events import CaptureEvent, EventBatch


def _event(i: int, minute: int) -> CaptureEvent:
    return CaptureEvent(
        event_id=f"evt_{i:04d}",
        timestamp=(datetime(2026, 7, 5, 21, 0, 0) + timedelta(minutes=minute)).isoformat(
            timespec="seconds"),
        app_name="Safari",
        window_title="YouTube",
        kind="heartbeat" if i > 1 else "switch",
    )


def _frame(busy: bool, seed: int) -> Image.Image:
    im = Image.new("RGB", (200, 130), (200, 180, 150))
    if busy:  # meaningfully different content per seed (a playing video / scrolling page)
        d = ImageDraw.Draw(im)
        d.rectangle([10 + (seed * 23) % 120, 30, 80 + (seed * 23) % 120, 110],
                    fill=(seed * 40 % 255, 30, 200))
    return im


def test_static_run_flagged_and_active_frames_pass(tmp_path):
    events = [_event(i, i) for i in range(1, 7)]
    # Frames 1-3: video actually playing (changes). Frames 4-6: paused — identical pixels.
    for i, e in enumerate(events, start=1):
        frame = _frame(busy=True, seed=i) if i <= 3 else _frame(busy=False, seed=0)
        frame.save(tmp_path / f"{e.event_id}.jpg", "JPEG", quality=85)
    batch = EventBatch(batch_id="b", events=tuple(events))

    changed = annotate_screen_changes(batch, tmp_path)
    assert changed["evt_0001"] is True          # first frame: nothing to compare against
    assert changed["evt_0002"] and changed["evt_0003"]
    assert changed["evt_0004"] is True          # playing -> paused is itself a change
    assert changed["evt_0005"] is False         # ...and then nothing moves
    assert changed["evt_0006"] is False


def test_missing_images_never_fabricate_idleness(tmp_path):
    events = [_event(i, i) for i in range(1, 4)]  # no .jpg files on disk at all
    batch = EventBatch(batch_id="b", events=tuple(events))
    changed = annotate_screen_changes(batch, tmp_path)
    assert all(changed.values())


def test_long_static_run_is_dropped_as_a_gap_in_code():
    # 3 min of real watching, then 30 static minutes (paused video), then 4 active min.
    # However the model groups it, the 30 static minutes must be DROPPED — away time is a
    # clean gap, not a journaled block. The rule is deterministic, not a prompt suggestion.
    from daylook.agents.analyst import (
        _Group, _Grouping, derive_blocks, enforce_static_breaks,
    )
    events = [_event(i, i) for i in range(1, 38)]
    batch = EventBatch(batch_id="b", events=tuple(events))
    grouping = _Grouping(blocks=[_Group(
        label="Watching a video", category="entertainment",
        detail="One long video.", event_ids=[e.event_id for e in events],
    )])
    changes = {e.event_id: True for e in events[:3]}
    changes.update({e.event_id: False for e in events[3:33]})
    changes.update({e.event_id: True for e in events[33:]})

    analysis = derive_blocks(batch, grouping)
    fixed = enforce_static_breaks(analysis, list(events), changes)

    # Only the two active runs survive; the static middle is gone (a gap between them).
    assert [b.category for b in fixed.blocks] == ["entertainment", "entertainment"]
    assert not any(b.label.startswith("Away") for b in fixed.blocks)
    # Anti-hallucination invariant survives: surviving blocks reference only real events, and
    # the dropped away events (the static middle) intentionally belong to no block.
    covered = sorted(eid for b in fixed.blocks for eid in b.source_event_ids)
    active_ids = sorted(e.event_id for e in [*events[:3], *events[33:]])
    assert covered == active_ids


def test_idle_batch_skips_the_vision_call_entirely(monkeypatch, tmp_path):
    # A whole batch that never visibly changed must produce NO blocks (away time is a clean
    # gap) with ZERO model calls — the token-frugality win. We assert llm_grouping is never
    # invoked, and that the batch contributes nothing to the timeline.
    from daylook.agents import analyst
    from daylook.agents.analyst import analyze_batch

    events = [_event(i, i * 12) for i in range(1, 8)]  # ~72 min span, well over threshold
    for e in events:  # identical static frames on disk
        _frame(busy=False, seed=0).save(tmp_path / f"{e.event_id}.jpg", "JPEG", quality=85)
    batch = EventBatch(batch_id="idle", events=tuple(events))

    def boom(*a, **k):
        raise AssertionError("llm_grouping was called for a provably-idle batch")
    monkeypatch.setattr(analyst, "llm_grouping", boom)

    result = analyze_batch(batch, pending_dir=tmp_path, all_events=events)
    assert result.blocks == []


def test_short_static_pause_is_left_to_the_model():
    # 4 static minutes mid-video (buffering, a long still shot) stays one block: under the
    # 10-minute bar, idleness is not provable and the model's grouping stands.
    from daylook.agents.analyst import (
        _Group, _Grouping, derive_blocks, enforce_static_breaks,
    )
    events = [_event(i, i) for i in range(1, 11)]
    batch = EventBatch(batch_id="b", events=tuple(events))
    grouping = _Grouping(blocks=[_Group(
        label="Watching a video", category="entertainment", event_ids=[e.event_id for e in events],
    )])
    changes = {e.event_id: True for e in events}
    for e in events[4:8]:
        changes[e.event_id] = False

    fixed = enforce_static_breaks(derive_blocks(batch, grouping), list(events), changes)
    assert len(fixed.blocks) == 1
    assert fixed.blocks[0].category == "entertainment"
