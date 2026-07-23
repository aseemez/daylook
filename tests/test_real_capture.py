"""Tests for the real macOS capture layer (build step 10).

These exercise the parts that don't touch the screen: the debounce state machine (which is why
it's a pure function), event construction, and the on-disk write shape (fed a synthetic PIL
image). The OS backend (``mss``/``pyobjc``) is imported lazily inside ``run()``, so none of
these tests need those deps installed — importing the module is always safe.
"""

from __future__ import annotations

import json
from datetime import datetime

from PIL import Image

from daylook import config
from daylook.capture_script import real_capture as rc
from daylook.events import CaptureEvent, load_event


APP = ("Cursor", "auth.py — acme-api")
OTHER = ("Google Chrome", "Stack Overflow")


def test_debounce_suppresses_flicker():
    """A window seen for less than the debounce window never emits."""
    state = rc.DebounceState()
    state, emit = rc.advance(state, APP, now=0.0, debounce_seconds=3.0)
    assert emit is False                      # first sighting: candidate, not committed
    state, emit = rc.advance(state, OTHER, now=1.0, debounce_seconds=3.0)
    assert emit is False                      # flicked away before 3s — nothing emitted
    assert state.candidate == OTHER


def test_debounce_emits_after_persist():
    """A window that persists past the debounce window emits exactly once."""
    state = rc.DebounceState()
    state, _ = rc.advance(state, APP, now=0.0, debounce_seconds=3.0)
    state, emit = rc.advance(state, APP, now=2.0, debounce_seconds=3.0)
    assert emit is False                      # only 2s in
    state, emit = rc.advance(state, APP, now=3.0, debounce_seconds=3.0)
    assert emit is True                       # crossed 3s — commit
    assert state.committed == APP
    # staying on the committed window does not re-emit
    state, emit = rc.advance(state, APP, now=10.0, debounce_seconds=3.0)
    assert emit is False


def test_returning_to_committed_cancels_candidate():
    """Bouncing to another window and back before it commits emits nothing."""
    state = rc.DebounceState(committed=APP)
    state, emit = rc.advance(state, OTHER, now=0.0, debounce_seconds=3.0)
    assert emit is False and state.candidate == OTHER
    state, emit = rc.advance(state, APP, now=1.0, debounce_seconds=3.0)
    assert emit is False                      # back on committed
    assert state.candidate is None            # candidate cleared


def test_make_event_id_is_sortable_and_matches_timestamp():
    when = datetime(2026, 7, 5, 9, 15, 30)
    event = rc.make_event("Cursor", "auth.py", when)
    assert event.event_id == "evt_20260705T091530"
    assert event.timestamp == "2026-07-05T09:15:30"
    assert event.app_name == "Cursor"


def test_write_event_matches_on_disk_contract(tmp_path):
    """write_event must produce the same sidecar+JPEG pair the pipeline expects."""
    event = rc.make_event("Cursor", "auth.py — acme-api", datetime(2026, 7, 5, 9, 15, 30))
    # a deliberately oversized image to prove downscaling to IMAGE_MAX_EDGE
    big = Image.new("RGB", (3000, 1800), (20, 20, 20))
    rc.write_event(event, big, tmp_path)

    sidecar = event.sidecar_path(tmp_path)
    image = event.image_path(tmp_path)
    assert sidecar.exists() and image.exists()

    # schema is exactly the shared contract, and round-trips
    data = json.loads(sidecar.read_text())
    assert set(data) == {"event_id", "timestamp", "app_name", "window_title", "kind"}
    assert load_event(sidecar) == event
    assert isinstance(CaptureEvent.from_dict(data), CaptureEvent)

    with Image.open(image) as im:
        assert max(im.size) <= config.IMAGE_MAX_EDGE


# --- capture health: blindness detection ----------------------------------------------
# Why this exists: macOS silently revokes Screen Recording when the app's code signature
# changes (every ad-hoc rebuild). Capture then "works" but records only the wallpaper — two
# days of real usage were lost to exactly that. These tests pin the detection logic.

def test_blindness_detector_trips_on_identical_switch_frames():
    d = rc.BlindnessDetector(threshold=3)
    d.observe("switch", "aaaa")               # first frame: nothing to compare against
    for _ in range(3):
        d.observe("switch", "aaaa")           # window changed, pixels didn't — impossible
    assert d.blind is True


def test_blindness_detector_ignores_static_heartbeats():
    """Reading the same page for minutes = identical heartbeat frames. That's normal and
    must never be flagged as blindness."""
    d = rc.BlindnessDetector(threshold=3)
    d.observe("switch", "aaaa")
    for _ in range(20):
        d.observe("heartbeat", "aaaa")
    assert d.blind is False


def test_blindness_detector_resets_on_changed_frame():
    """Re-granting permission mid-run makes frames differ again — warning must clear."""
    d = rc.BlindnessDetector(threshold=3)
    d.observe("switch", "aaaa")
    d.observe("switch", "aaaa")
    d.observe("switch", "aaaa")
    d.observe("switch", "bbbb")               # a real frame change
    assert d.identical_switches == 0
    for _ in range(3):
        d.observe("switch", "bbbb")
    assert d.blind is True                    # and it can trip again afterwards


def test_write_event_returns_stable_frame_hash(tmp_path):
    img = Image.new("RGB", (100, 60), (10, 20, 30))
    e1 = rc.make_event("Cursor", "a", datetime(2026, 7, 5, 9, 0, 0))
    e2 = rc.make_event("Chrome", "b", datetime(2026, 7, 5, 9, 0, 5))
    h1 = rc.write_event(e1, img, tmp_path)
    h2 = rc.write_event(e2, img.copy(), tmp_path)
    assert h1 == h2                           # same pixels -> same hash (the blindness signal)
    assert rc.write_event(e1, Image.new("RGB", (100, 60), (200, 0, 0)), tmp_path) != h1


def test_health_file_roundtrip_and_status_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    d = rc.BlindnessDetector(threshold=1)
    d.observe("switch", "x")
    d.observe("switch", "x")
    assert d.blind
    rc.write_health(permission_granted=True, detector=d, running=True)
    h = rc.read_health()
    assert h is not None and h["blind"] is True and h["permission_granted"] is True

    # permission denied alone also means blind, even with no frozen frames yet
    rc.write_health(permission_granted=False, detector=rc.BlindnessDetector(), running=True)
    assert rc.read_health()["blind"] is True


def test_read_health_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    assert rc.read_health() is None
