"""Unit tests for analyst.merge_blocks — the de-fragmentation that turns a minute-by-minute
log into a readable journal. Pure function over block dicts, no model calls."""

from __future__ import annotations

from daylook.agents.analyst import merge_blocks


def _b(start, end, dur, label, cat, app, ids):
    return {
        "start": start, "end": end, "duration_seconds": dur,
        "label": label, "category": cat, "app_name": app, "source_event_ids": ids,
    }


def test_merges_adjacent_same_app_and_category():
    blocks = [
        _b("09:00", "09:10", 600, "Coding A", "coding", "Cursor", ["e1"]),
        _b("09:10", "09:25", 900, "Coding B", "coding", "Cursor", ["e2"]),
    ]
    out = merge_blocks(blocks, min_seconds=120)
    assert len(out) == 1
    # longer block keeps identity; times and ids are unioned
    assert out[0]["label"] == "Coding B"
    assert out[0]["duration_seconds"] == 1500
    assert out[0]["start"] == "09:00" and out[0]["end"] == "09:25"
    assert out[0]["source_event_ids"] == ["e1", "e2"]


def test_does_not_merge_different_app_when_both_long():
    blocks = [
        _b("09:00", "09:20", 1200, "Coding", "coding", "Cursor", ["e1"]),
        _b("09:20", "09:40", 1200, "Research", "research", "Chrome", ["e2"]),
    ]
    out = merge_blocks(blocks, min_seconds=120)
    assert len(out) == 2


def test_folds_short_blip_into_larger_neighbour():
    # a 30s Safari glance in the middle of two long Terminal sessions disappears into them
    blocks = [
        _b("09:00", "09:20", 1200, "Deep work", "coding", "Terminal", ["e1"]),
        _b("09:20", "09:20", 30, "Quick peek", "research", "Safari", ["e2"]),
        _b("09:20", "09:40", 1200, "Deep work", "coding", "Terminal", ["e3"]),
    ]
    out = merge_blocks(blocks, min_seconds=120)
    assert len(out) == 1
    assert out[0]["app_name"] == "Terminal"
    assert out[0]["category"] == "coding"
    assert "e2" in out[0]["source_event_ids"]  # time still traces back to the real event
    assert out[0]["duration_seconds"] == 2430


def test_keeps_a_lone_short_day():
    blocks = [_b("09:00", "09:01", 60, "Quick check", "other", "Mail", ["e1"])]
    out = merge_blocks(blocks, min_seconds=120)
    assert len(out) == 1  # nothing to fold into — don't drop the only block
