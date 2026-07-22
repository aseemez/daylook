"""HTTP capture control — the endpoints behind the always-visible recording pill.

Hermetic: a fake Root is injected so no screen is ever touched. The RootAgent's own
start/stop/status lifecycle is covered by test_capture_control.py; this only checks the wiring.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from daylook.api.main import app, get_root


class _FakeRoot:
    def __init__(self):
        self.running = False

    def capture_status(self):
        return {"running": self.running, "elapsed_seconds": 120 if self.running else 0}

    def start_capture_process(self, minutes: int = 0):
        self.running = True
        return {"status": "started"}

    def stop_capture_process(self):
        self.running = False
        return {"status": "stopped"}


def test_capture_status_start_stop():
    fake = _FakeRoot()
    app.dependency_overrides[get_root] = lambda: fake
    try:
        c = TestClient(app)

        assert c.get("/capture/status").json() == {"running": False, "elapsed_seconds": 0}

        assert c.post("/capture/start", json={}).json()["status"] == "started"
        st = c.get("/capture/status").json()
        assert st["running"] is True and st["elapsed_seconds"] == 120

        assert c.post("/capture/stop").json()["status"] == "stopped"
        assert c.get("/capture/status").json()["running"] is False
    finally:
        app.dependency_overrides.clear()
