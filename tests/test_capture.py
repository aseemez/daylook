"""Tests for the Capture Agent (build step 2).

Covers the deterministic batching rules and the read/move lifecycle against real mock events.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from daylook.agents.capture import CaptureAgent, batch_events
from daylook.capture_script.mock_generator import generate
from daylook.events import CaptureEvent


def _events(times_seconds: list[int]) -> list[CaptureEvent]:
    base = datetime(2026, 7, 5, 9, 0, 0)
    out = []
    for i, s in enumerate(times_seconds, start=1):
        out.append(CaptureEvent(
            event_id=f"evt_{i:04d}",
            timestamp=(base + timedelta(seconds=s)).isoformat(timespec="seconds"),
            app_name="Cursor",
            window_title="x.py",
        ))
    return out


def test_empty_input_yields_no_batches():
    assert batch_events([]) == []


def test_idle_gap_splits_batches():
    # two events close together, then a 30-min gap, then two more
    evs = _events([0, 60, 60 + 30 * 60, 60 + 30 * 60 + 60])
    batches = batch_events(evs, idle_gap_seconds=20 * 60)
    assert len(batches) == 2
    assert len(batches[0]) == 2
    assert len(batches[1]) == 2


def test_max_events_cap_splits_batches():
    evs = _events([i * 10 for i in range(10)])  # 10 events, 10s apart, no idle gap
    batches = batch_events(evs, idle_gap_seconds=10_000, max_events=4)
    assert [len(b) for b in batches] == [4, 4, 2]


def test_span_cap_splits_batches():
    # events every 10 min; a 25-min span cap should force splits before the idle gap fires
    evs = _events([i * 600 for i in range(6)])
    batches = batch_events(evs, idle_gap_seconds=10_000, max_events=100, max_span_seconds=25 * 60)
    # each batch spans at most 25 min => at most 3 events (0,10,20 min) per batch
    assert all((b.end - b.start).total_seconds() <= 25 * 60 for b in batches)
    assert len(batches) >= 2


def test_batch_ids_are_unique_and_ordered():
    evs = _events([0, 60, 60 + 30 * 60])
    batches = batch_events(evs, idle_gap_seconds=20 * 60)
    ids = [b.batch_id for b in batches]
    assert len(set(ids)) == len(ids)
    assert batches[0].start < batches[1].start


def test_read_and_process_lifecycle(tmp_path):
    pending = tmp_path / "pending"
    processed = tmp_path / "processed"
    generate(pending=pending, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)

    agent = CaptureAgent(pending=pending, processed=processed)
    assert agent.pending_count() == 30

    batches = agent.batch_pending()
    assert len(batches) >= 1
    # every event ends up in exactly one batch, none dropped or duplicated
    all_ids = [eid for b in batches for eid in b.event_ids]
    assert len(all_ids) == 30
    assert len(set(all_ids)) == 30

    # process the first batch — its files move out of pending into processed
    first = batches[0]
    moved = agent.mark_processed(first)
    assert set(moved) == set(first.event_ids)
    for eid in first.event_ids:
        assert not (pending / f"{eid}.json").exists()
        assert (processed / f"{eid}.json").exists()
        assert (processed / f"{eid}.jpg").exists()

    assert agent.pending_count() == 30 - len(first)
