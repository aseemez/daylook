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

from contextline import config
from contextline.capture_script import real_capture as rc
from contextline.events import CaptureEvent, load_event


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
    assert set(data) == {"event_id", "timestamp", "app_name", "window_title"}
    assert load_event(sidecar) == event
    assert isinstance(CaptureEvent.from_dict(data), CaptureEvent)

    with Image.open(image) as im:
        assert max(im.size) <= config.IMAGE_MAX_EDGE
