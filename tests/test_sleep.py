"""System sleep/wake auto-pause: a sleeping Mac must record nothing (no events, no tokens),
then resume on its own at wake — without clearing a deliberate user pause.

The launcher sets ``system_pause_event`` on ``NSWorkspaceWillSleepNotification`` and clears it
on ``DidWake``; here we drive that event from a tick-scripted fake so no real screen or sleep is
involved (hermetic, like ``test_screen_off``).
"""

from __future__ import annotations

import threading
import time

from PIL import Image

from daylook.capture_script import real_capture


class _Clock:
    """Wall-clock scripted fake (the loop's ``_wait`` advances real time even while paused, so
    unlike a tick counter this keeps progressing during the sleep window). Frontmost is
    "Before" until 0.30s in, then "After"; the fake sleep event is set for a middle window."""

    def __init__(self):
        self.t0 = time.monotonic()

    def _elapsed(self):
        return time.monotonic() - self.t0

    def frontmost(self):
        t = self._elapsed()
        if t < 0.05:
            return ("Before", "w1")
        if t < 0.20:
            return ("During", "w2")   # a window change WHILE asleep — must never be captured
        return ("After", "w3")

    def grab(self):
        return Image.new("RGB", (32, 20), (int(self._elapsed() * 500) % 256, 90, 40))

    class _Ev:
        def __init__(self, clock):
            self.clock = clock
        def is_set(self):
            return 0.05 <= self.clock._elapsed() < 0.20  # asleep in the middle

    def sleep_event(self):
        return self._Ev(self)


def test_sleep_suspends_capture_and_wake_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    clock = _Clock()
    monkeypatch.setattr(
        real_capture, "_load_backend",
        lambda need_screenshots=True: real_capture._Backend(
            frontmost=clock.frontmost, grab_primary=clock.grab, close=lambda: None),
    )
    monkeypatch.setattr(real_capture, "preflight_screen_permission", lambda: True)
    monkeypatch.setattr(real_capture, "screen_unavailable", lambda: False)

    # Unique ids: test polls are far faster than the real >=1s cadence, so two events could
    # otherwise share a timestamp-derived id.
    real_make = real_capture.make_event
    seq = iter(range(1000))
    monkeypatch.setattr(real_capture, "make_event", lambda a, t, w, kind="switch": (
        (lambda ev: type(ev)(**{**ev.__dict__, "event_id": f"{ev.event_id}_{next(seq):03d}"}))(
            real_make(a, t, w, kind=kind))))

    states: list[str] = []
    real_write = real_capture.write_health
    monkeypatch.setattr(real_capture, "write_health",
                        lambda p, d, running, state="recording": (
                            states.append(state if running else "stopped"),
                            real_write(p, d, running, state)))

    written = real_capture.run(
        pending=tmp_path / "pending",
        poll_seconds=0.005, debounce_seconds=0.001,
        heartbeat_seconds=0, stale_pause_seconds=0,
        system_pause_event=clock.sleep_event(),
        max_events=2, duration_seconds=5, stop_event=threading.Event(),
    )

    # The window that changed WHILE asleep ("During") must never appear — proof that a
    # sleeping Mac records nothing. Before/After straddle the sleep.
    apps = [real_capture.json.loads(p.read_text())["app_name"]
            for p in sorted((tmp_path / "pending").glob("*.json"))]
    assert "During" not in apps
    assert apps == ["Before", "After"]
    # Sleep was reported, then recording resumed.
    assert "system_sleep" in states
    assert "recording" in states[states.index("system_sleep"):]


def test_manual_pause_outranks_system_in_reported_reason(tmp_path, monkeypatch):
    # When both a user pause and a system sleep are set, the UI must be told "paused" (user),
    # never "system_sleep" — a wake must not look like it could clear the user's hold.
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    clock = _Clock()
    monkeypatch.setattr(
        real_capture, "_load_backend",
        lambda need_screenshots=True: real_capture._Backend(
            frontmost=clock.frontmost, grab_primary=clock.grab, close=lambda: None),
    )
    monkeypatch.setattr(real_capture, "preflight_screen_permission", lambda: True)
    monkeypatch.setattr(real_capture, "screen_unavailable", lambda: False)

    always = threading.Event(); always.set()          # manual pause held the whole run
    states: list[str] = []
    real_write = real_capture.write_health
    monkeypatch.setattr(real_capture, "write_health",
                        lambda p, d, running, state="recording": (
                            states.append(state), real_write(p, d, running, state)))

    real_capture.run(
        pending=tmp_path / "pending", poll_seconds=0.005, debounce_seconds=0.001,
        heartbeat_seconds=0, stale_pause_seconds=0,
        pause_event=always, system_pause_event=clock.sleep_event(),
        duration_seconds=0.2, stop_event=threading.Event(),
    )
    assert "paused" in states
    assert "system_sleep" not in states
