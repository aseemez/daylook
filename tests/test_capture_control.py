"""Unit tests for the Root Agent's real-capture control.

Capture runs on a background thread *inside the app process* (so macOS keeps the Screen
Recording grant — see RootAgent.start_capture_process). Hermetic: we fake ``real_capture.run``
so no screen is ever touched; the fake blocks on the stop Event, mimicking the real loop, so we
can assert the start/stop/idempotency lifecycle and the arguments passed through.
"""

from __future__ import annotations

from daylook.agents.root import RootAgent


def _patch_run(monkeypatch):
    """Replace the capture loop with one that just parks until stop_event is set."""
    calls: list[dict] = []

    def fake_run(pending=None, dry_run=False, duration_seconds=None, stop_event=None, **kw):
        calls.append({"dry_run": dry_run, "duration_seconds": duration_seconds})
        if stop_event is not None:
            stop_event.wait()  # stay alive like the real loop until asked to stop
        return 0

    monkeypatch.setattr("daylook.capture_script.real_capture.run", fake_run)
    return calls


def test_capture_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    calls = _patch_run(monkeypatch)
    root = RootAgent()

    # idle to begin with; stopping when idle is a safe no-op
    assert root.capture_status()["running"] is False
    assert root.stop_capture_process()["status"] == "not_running"

    # start with a duration -> one thread, duration forwarded as seconds
    started = root.start_capture_process(minutes=5)
    assert started["status"] == "started"
    assert root.capture_status()["running"] is True
    assert calls[-1]["duration_seconds"] == 300
    assert calls[-1]["dry_run"] is False

    # idempotent: a second start while running does not spawn again
    assert root.start_capture_process()["status"] == "already_running"
    assert len(calls) == 1

    # stop -> thread joins, back to idle
    stopped = root.stop_capture_process()
    assert stopped["status"] == "stopped"
    assert root.capture_status()["running"] is False
    assert root.stop_capture_process()["status"] == "not_running"


def test_dry_run_flag_passed(monkeypatch, tmp_path):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    calls = _patch_run(monkeypatch)
    root = RootAgent()

    root.start_capture_process(dry_run=True)
    try:
        assert calls[-1]["dry_run"] is True
        assert calls[-1]["duration_seconds"] is None  # 0 minutes -> run until stopped
    finally:
        root.stop_capture_process()


def test_capture_status_surfaces_blindness_only_while_running(monkeypatch, tmp_path):
    """The UI warning keys off status['blind']: it must fire during a blind capture and go
    quiet once capture stops (a stale health file must not nag after the user fixed it)."""
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    from daylook.capture_script import real_capture as rc

    _patch_run(monkeypatch)
    root = RootAgent()

    rc.write_health(permission_granted=False, detector=None, running=True)
    assert root.capture_status()["blind"] is False   # not running -> no warning

    root.start_capture_process()
    assert root.capture_status()["blind"] is True    # running + blind health -> warn
    assert root.capture_status()["health"]["permission_granted"] is False

    root.stop_capture_process()
    assert root.capture_status()["blind"] is False


def test_pause_resume_lifecycle(monkeypatch, tmp_path):
    """Pause holds the session (thread alive, event set); resume clears it; both are safe
    no-ops when idle. The forgotten-pause failure mode is handled UI-side (persistent
    'Paused' pill fed by capture_status)."""
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    seen: dict = {}

    def fake_run(pending=None, dry_run=False, duration_seconds=None,
                 stop_event=None, pause_event=None, **kw):
        seen["pause_event"] = pause_event
        if stop_event is not None:
            stop_event.wait()
        return 0

    monkeypatch.setattr("daylook.capture_script.real_capture.run", fake_run)
    root = RootAgent()

    assert root.pause_capture_process()["status"] == "not_running"
    assert root.resume_capture_process()["status"] == "not_running"

    root.start_capture_process()
    assert seen["pause_event"] is not None, "capture loop was not handed the pause event"

    assert root.pause_capture_process()["status"] == "paused"
    st = root.capture_status()
    assert st["running"] is True and st["paused"] is True
    assert seen["pause_event"].is_set()

    assert root.resume_capture_process()["status"] == "recording"
    assert root.capture_status()["paused"] is False
    assert not seen["pause_event"].is_set()

    root.stop_capture_process()
    assert root.capture_status()["paused"] is False
