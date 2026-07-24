"""Instant auto-pause when the screen is locked / the display sleeps.

The staleness detector needs 10 frozen minutes to conclude the chair is empty; a lock or a
sleeping display is a definitive OS signal and must pause capture *immediately* (the 07-23
evening was journaled as a 92-minute video session because heartbeats kept firing against a
dark screen). Hermetic: the OS backend and the lock predicate are injected, so no screen or
Quartz is touched.
"""

from __future__ import annotations

import threading

from PIL import Image

from daylook.capture_script import real_capture


class _Script:
    """Deterministic world for one run(): ticks advance on each screen-off poll (the loop's
    first act every cycle), the frontmost app changes after the screen comes back, and every
    grabbed frame is unique so the staleness clock never interferes."""

    def __init__(self):
        self.ticks = 0
        self.grabs = 0

    def screen_off(self) -> bool:
        self.ticks += 1
        return 12 <= self.ticks < 24  # a lock in the middle of the run

    def frontmost(self):
        return ("After", "w2") if self.ticks >= 24 else ("Before", "w1")

    def grab(self):
        self.grabs += 1
        return Image.new("RGB", (32, 20), (self.grabs % 256, 120, 40))


def test_lock_pauses_instantly_and_resume_re_debounces(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    script = _Script()
    monkeypatch.setattr(
        real_capture, "_load_backend",
        lambda need_screenshots=True: real_capture._Backend(
            frontmost=script.frontmost, grab_primary=script.grab, close=lambda: None),
    )
    monkeypatch.setattr(real_capture, "preflight_screen_permission", lambda: True)

    # Test polls are 5ms apart, so two events can land in the same wall-clock second and
    # collide on the timestamp-derived id (impossible at the real >=1s poll cadence).
    # Suffix a counter to keep each test event distinct on disk.
    real_make = real_capture.make_event
    seq = iter(range(1000))

    def unique_event(app_name, window_title, when, kind="switch"):
        ev = real_make(app_name, window_title, when, kind=kind)
        return type(ev)(**{**ev.__dict__, "event_id": f"{ev.event_id}_{next(seq):03d}"})

    monkeypatch.setattr(real_capture, "make_event", unique_event)

    states: list[str] = []
    real_write = real_capture.write_health

    def spy_health(permission, detector, running, state="recording"):
        states.append(state if running else "stopped")
        real_write(permission, detector, running, state)

    monkeypatch.setattr(real_capture, "write_health", spy_health)

    written = real_capture.run(
        pending=tmp_path / "pending",
        poll_seconds=0.005,
        debounce_seconds=0.001,
        heartbeat_seconds=0,          # heartbeats off: switches tell the whole story here
        stale_pause_seconds=0,        # staleness detector off: this test is about the lock
        screen_off_fn=script.screen_off,
        max_events=2,
        duration_seconds=10,          # hard safety stop; normally ends at max_events
        stop_event=threading.Event(),
    )

    # One switch before the lock, one after — and nothing at all in between.
    assert written == 2
    sidecars = sorted((tmp_path / "pending").glob("*.json"))
    apps = [real_capture.json.loads(p.read_text())["app_name"] for p in sidecars]
    assert apps == ["Before", "After"]

    # The lock was reported the moment it happened, and recording resumed after.
    assert "screen_off" in states
    resumed_after_lock = states[states.index("screen_off"):]
    assert "recording" in resumed_after_lock


def test_screen_unavailable_is_a_safe_bool():
    # On any host (Quartz present or not) this must return a bool and never raise —
    # capture degrades to the staleness detector rather than crashing.
    assert real_capture.screen_unavailable() in (True, False)
