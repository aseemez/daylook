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
    assert root.capture_status() == {"running": False}
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
