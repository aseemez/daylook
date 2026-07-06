"""Unit tests for the Root Agent's real-capture process control.

Hermetic: ``subprocess.Popen`` is faked, so no real capture process is spawned and the
screen is never touched. We only assert the start/stop lifecycle and command construction.
"""

from __future__ import annotations

import signal

from contextline.agents.root import RootAgent


class FakeProc:
    """Minimal stand-in for a Popen handle: alive until signaled/terminated."""

    def __init__(self):
        self.pid = 4242
        self._alive = True
        self.signals: list[int] = []

    def poll(self):
        return None if self._alive else 0

    def send_signal(self, sig):
        self.signals.append(sig)
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False


def _patch_popen(monkeypatch):
    created: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        created.append(cmd)
        return FakeProc()

    monkeypatch.setattr("contextline.agents.root.subprocess.Popen", fake_popen)
    return created


def test_capture_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    created = _patch_popen(monkeypatch)
    root = RootAgent()

    # idle to begin with; stopping when idle is a safe no-op
    assert root.capture_status() == {"running": False, "pid": None}
    assert root.stop_capture_process()["status"] == "not_running"

    # start with a duration -> spawns once, command carries --minutes
    started = root.start_capture_process(minutes=5)
    assert started["status"] == "started"
    assert root.capture_status()["running"] is True
    assert "--minutes" in created[-1] and "5" in created[-1]
    assert "--dry-run" not in created[-1]

    # idempotent: a second start while running does not spawn again
    assert root.start_capture_process()["status"] == "already_running"
    assert len(created) == 1

    # stop -> graceful SIGINT first, then idle
    stopped = root.stop_capture_process()
    assert stopped["status"] == "stopped"
    assert root.capture_status()["running"] is False
    assert root.stop_capture_process()["status"] == "not_running"


def test_dry_run_flag_passed(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    created = _patch_popen(monkeypatch)
    root = RootAgent()

    root.start_capture_process(dry_run=True)
    assert "--dry-run" in created[-1]
    assert "--minutes" not in created[-1]  # 0 minutes -> run until stopped


def test_stop_sends_sigint(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    _patch_popen(monkeypatch)
    root = RootAgent()

    root.start_capture_process()
    proc = root._capture_proc
    root.stop_capture_process()
    assert signal.SIGINT in proc.signals
