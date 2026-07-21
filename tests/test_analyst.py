"""Tests for the Analyst's deterministic derivation (build step 3).

These exercise ``derive_blocks`` directly with hand-made groupings — no model calls — so they
are fast, free, and offline. They pin the guarantees ``eval.py`` relies on: full coverage,
no hallucinated ids, real derived times/apps. The live vision call is covered separately in
``test_analyst_llm.py`` (opt-in).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from daylook.agents.analyst import (
    _Group,
    _Grouping,
    compute_dwell,
    derive_blocks,
)
from daylook.events import CaptureEvent, EventBatch


def _batch(specs: list[tuple[str, int]]) -> EventBatch:
    """specs = [(app, minute_offset), ...] -> an EventBatch of evt_0001.. at 09:00+offset."""
    base = datetime(2026, 7, 5, 9, 0, 0)
    events = []
    for i, (app, off) in enumerate(specs, start=1):
        events.append(CaptureEvent(
            event_id=f"evt_{i:04d}",
            timestamp=(base + timedelta(minutes=off)).isoformat(timespec="seconds"),
            app_name=app,
            window_title=f"{app} window",
        ))
    return EventBatch(batch_id="batch_test", events=tuple(events))


def test_full_coverage_every_event_in_one_block():
    batch = _batch([("Cursor", 0), ("Cursor", 5), ("Chrome", 10), ("Chrome", 12)])
    grouping = _Grouping(blocks=[
        _Group(label="Coding", category="coding", event_ids=["evt_0001", "evt_0002"]),
        _Group(label="Browsing", category="research", event_ids=["evt_0003", "evt_0004"]),
    ])
    analysis = derive_blocks(batch, grouping)
    covered = [eid for b in analysis.blocks for eid in b.source_event_ids]
    assert sorted(covered) == ["evt_0001", "evt_0002", "evt_0003", "evt_0004"]
    assert len(covered) == len(set(covered))  # no duplicates


def test_hallucinated_event_ids_are_ignored():
    batch = _batch([("Cursor", 0), ("Cursor", 5)])
    grouping = _Grouping(blocks=[
        _Group(label="Coding", category="coding",
               event_ids=["evt_0001", "evt_0002", "evt_9999"]),  # 9999 doesn't exist
    ])
    analysis = derive_blocks(batch, grouping)
    all_ids = {eid for b in analysis.blocks for eid in b.source_event_ids}
    assert all_ids == {"evt_0001", "evt_0002"}
    assert "evt_9999" not in all_ids


def test_unassigned_events_are_not_dropped():
    batch = _batch([("Cursor", 0), ("Cursor", 5), ("Chrome", 10)])
    # LLM forgot evt_0003 entirely
    grouping = _Grouping(blocks=[
        _Group(label="Coding", category="coding", event_ids=["evt_0001", "evt_0002"]),
    ])
    analysis = derive_blocks(batch, grouping)
    covered = {eid for b in analysis.blocks for eid in b.source_event_ids}
    assert covered == {"evt_0001", "evt_0002", "evt_0003"}


def test_blocks_only_contain_consecutive_events():
    batch = _batch([("Cursor", 0), ("Chrome", 5), ("Cursor", 10)])
    # LLM tries to merge the two non-adjacent Cursor events into one block
    grouping = _Grouping(blocks=[
        _Group(label="Coding", category="coding", event_ids=["evt_0001", "evt_0003"]),
        _Group(label="Browsing", category="research", event_ids=["evt_0002"]),
    ])
    analysis = derive_blocks(batch, grouping)
    # walking in order, the Chrome event splits the two Cursor events into separate blocks
    for b in analysis.blocks:
        ids = b.source_event_ids
        nums = [int(x.split("_")[1]) for x in ids]
        assert nums == list(range(nums[0], nums[0] + len(nums)))  # each block is contiguous


def test_duration_derived_from_timestamps_not_model():
    # events at 0 and 5 minutes; dwell of first = 5 min, last = median(=5 min)
    batch = _batch([("Cursor", 0), ("Cursor", 5)])
    grouping = _Grouping(blocks=[
        _Group(label="Coding", category="coding", event_ids=["evt_0001", "evt_0002"]),
    ])
    analysis = derive_blocks(batch, grouping)
    block = analysis.blocks[0]
    assert block.duration_seconds == 5 * 60 + 5 * 60  # first dwell + last default
    assert block.app_name == "Cursor"


def test_app_name_is_dominant_real_app():
    batch = _batch([("Cursor", 0), ("Cursor", 5), ("Slack", 10)])
    grouping = _Grouping(blocks=[
        _Group(label="Mixed", category="coding",
               event_ids=["evt_0001", "evt_0002", "evt_0003"]),
    ])
    analysis = derive_blocks(batch, grouping)
    assert analysis.blocks[0].app_name == "Cursor"  # 2 Cursor vs 1 Slack


def test_time_distributions():
    batch = _batch([("Cursor", 0), ("Chrome", 5), ("Chrome", 10)])
    grouping = _Grouping(blocks=[
        _Group(label="Coding", category="coding", event_ids=["evt_0001"]),
        _Group(label="Research", category="research", event_ids=["evt_0002", "evt_0003"]),
    ])
    analysis = derive_blocks(batch, grouping)
    cats = analysis.time_by_category()
    assert set(cats.keys()) == {"coding", "research"}
    assert all(v > 0 for v in cats.values())


def test_dwell_caps_idle_gaps():
    # a 2-hour gap between two events must not attribute 2 hours to the first
    batch = _batch([("Cursor", 0), ("Cursor", 120)])
    dwell = compute_dwell(list(batch.events))
    from daylook import config
    assert dwell["evt_0001"] == config.MAX_EVENT_DWELL_SECONDS


def test_invalid_category_falls_back_to_other():
    batch = _batch([("Cursor", 0)])
    grouping = _Grouping(blocks=[
        _Group(label="Weird", category="not-a-real-category", event_ids=["evt_0001"]),
    ])
    analysis = derive_blocks(batch, grouping)
    assert analysis.blocks[0].category == "other"
