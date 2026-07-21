"""Tests for stable-work heartbeats — the capture cadence added for exact time accounting.

Three seams, all deterministic and screen-free:
  * the capture loop's pure heartbeat decision (:func:`real_capture.heartbeat_due`),
  * the Capture Agent's switch-only batching cap (heartbeats must not shatter batches),
  * the Analyst's keyframe selection (bounded images per vision call).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from daylook import config
from daylook.agents.analyst import select_keyframes
from daylook.agents.capture import batch_events
from daylook.capture_script.mock_generator import SCENARIO, generate
from daylook.capture_script.real_capture import DebounceState, heartbeat_due, make_event
from daylook.events import CaptureEvent, EventBatch


def _stream(spec: list[tuple[int, str]]) -> list[CaptureEvent]:
    """Events from (seconds_offset, kind) pairs; app varies per switch so they're distinct."""
    base = datetime(2026, 7, 5, 9, 0, 0)
    out, switch_n = [], 0
    for i, (s, kind) in enumerate(spec, start=1):
        if kind == "switch":
            switch_n += 1
        out.append(CaptureEvent(
            event_id=f"evt_{i:04d}",
            timestamp=(base + timedelta(seconds=s)).isoformat(timespec="seconds"),
            app_name=f"App{switch_n}",
            window_title="w",
            kind=kind,
        ))
    return out


# --- capture loop decision -------------------------------------------------------------

def test_no_heartbeat_before_first_commit():
    state = DebounceState()  # nothing committed yet — user hasn't stably landed anywhere
    assert not heartbeat_due(state, last_emit_at=0.0, now=100.0, interval=12.0)


def test_heartbeat_fires_after_interval_on_committed_window():
    state = DebounceState(committed=("Cursor", "x.py"))
    assert not heartbeat_due(state, last_emit_at=100.0, now=111.0, interval=12.0)
    assert heartbeat_due(state, last_emit_at=100.0, now=112.0, interval=12.0)


def test_no_heartbeat_while_a_switch_candidate_is_pending():
    state = DebounceState(committed=("Cursor", "x.py"),
                          candidate=("Slack", "general"), candidate_since=110.0)
    assert not heartbeat_due(state, last_emit_at=100.0, now=120.0, interval=12.0)


def test_interval_zero_disables_heartbeats():
    state = DebounceState(committed=("Cursor", "x.py"))
    assert not heartbeat_due(state, last_emit_at=0.0, now=1e9, interval=0.0)


def test_make_event_carries_kind():
    e = make_event("Cursor", "x.py", datetime(2026, 7, 5, 9, 0, 0), kind="heartbeat")
    assert e.kind == "heartbeat"
    assert CaptureEvent.from_dict(e.to_dict()).kind == "heartbeat"


def test_kind_defaults_to_switch_for_legacy_sidecars():
    # pre-heartbeat sidecars have no "kind" — they must load as switches unchanged
    d = {"event_id": "evt_0001", "timestamp": "2026-07-05T09:00:00",
         "app_name": "Cursor", "window_title": "x.py"}
    assert CaptureEvent.from_dict(d).kind == "switch"


# --- batching --------------------------------------------------------------------------

def test_heartbeats_do_not_trip_the_switch_cap():
    # 2 switches with 50 heartbeats between them, 12s apart — must stay ONE batch
    spec = [(0, "switch")] + [(12 * i, "heartbeat") for i in range(1, 51)] + [(620, "switch")]
    batches = batch_events(_stream(spec), idle_gap_seconds=10_000, max_events=4)
    assert len(batches) == 1


def test_total_cap_still_bounds_heartbeat_floods():
    spec = [(0, "switch")] + [(i, "heartbeat") for i in range(1, 30)]
    batches = batch_events(_stream(spec), idle_gap_seconds=10_000,
                           max_events=12, max_total_events=10)
    assert all(len(b) <= 10 for b in batches)
    assert sum(len(b) for b in batches) == 30  # nothing dropped


def test_switch_only_stream_batches_exactly_as_before():
    spec = [(i * 10, "switch") for i in range(10)]
    batches = batch_events(_stream(spec), idle_gap_seconds=10_000, max_events=4)
    assert [len(b) for b in batches] == [4, 4, 2]  # same as the pre-heartbeat contract


# --- keyframe selection ----------------------------------------------------------------

def _batch(spec: list[tuple[int, str]]) -> EventBatch:
    evs = _stream(spec)
    return EventBatch(batch_id="batch_test", events=tuple(evs))


def test_every_switch_is_a_keyframe_when_under_cap():
    spec = [(0, "switch"), (12, "heartbeat"), (24, "heartbeat"), (300, "switch")]
    batch = _batch(spec)
    keys = select_keyframes(batch, max_images=12)
    switch_ids = {e.event_id for e in batch.events if e.kind == "switch"}
    assert switch_ids <= keys


def test_keyframes_respect_the_image_cap_with_heartbeat_sampling():
    spec = [(0, "switch")] + [(12 * i, "heartbeat") for i in range(1, 101)]
    keys = select_keyframes(_batch(spec), max_images=12)
    assert len(keys) <= 12
    assert "evt_0001" in keys                      # the switch
    assert any(k != "evt_0001" for k in keys)      # plus sampled heartbeats


def test_switch_heavy_batch_caps_at_max_images():
    spec = [(i * 30, "switch") for i in range(20)]
    keys = select_keyframes(_batch(spec), max_images=12)
    assert len(keys) == 12


def test_mock_generator_heartbeats_share_the_disk_shape(tmp_path):
    events = generate(pending=tmp_path, start=datetime(2026, 7, 5, 9, 0, 0),
                      scenario=SCENARIO[:2], heartbeat_seconds=60)
    kinds = {e.kind for e in events}
    assert kinds == {"switch", "heartbeat"}
    for e in events:
        assert e.sidecar_path(tmp_path).exists()
        assert e.image_path(tmp_path).exists()
    # ids sort in timestamp order (hb ids nest under their segment id)
    ids = [e.event_id for e in events]
    stamps = [e.timestamp for e in events]
    assert ids == [i for _, i in sorted(zip(stamps, ids))]
