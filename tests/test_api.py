"""Tests for the FastAPI wrapper (build step 9).

Hermetic: ``/health`` runs directly; ``/chat`` uses a fake Root injected via FastAPI's
dependency override, so no model calls. A live end-to-end API test is opt-in below.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from daylook.api.main import app, get_root


class _FakeRoot:
    async def chat(self, message: str, session_id: str | None = None):
        return f"echo:{message}", (session_id or "new-session")


def test_index_serves_chat_page():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "DayLook" in r.text


def test_health(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "model" in body
    assert body["pending_events"] in (0, None)


def test_setup_reports_unconfigured_then_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    for var in ("GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(var, raising=False)
    client = TestClient(app)

    r = client.get("/setup")
    assert r.status_code == 200
    assert r.json()["configured"] is False

    r = client.post("/setup", json={"invite_token": "cl_demo", "relay_url": "https://relay.test"})
    assert r.status_code == 200
    assert r.json()["configured"] is True

    # persisted, and applied to the live process so the next /chat is routed
    assert client.get("/setup").json()["configured"] is True
    assert os.environ["GOOGLE_API_KEY"] == "cl_demo"
    assert os.environ["GOOGLE_GEMINI_BASE_URL"] == "https://relay.test"


def test_setup_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    client = TestClient(app)
    r = client.post("/setup", json={})
    assert r.status_code == 422  # invite_token is required


def test_launcher_smoke(tmp_path, monkeypatch):
    """The build gate: --smoke boots the real server and confirms /health, returns 0."""
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))  # never touch the real home
    from daylook import launcher
    assert launcher.main(["--smoke", "--no-browser"]) == 0


def test_chat_creates_and_reuses_session():
    app.dependency_overrides[get_root] = lambda: _FakeRoot()
    try:
        client = TestClient(app)
        r = client.post("/chat", json={"message": "hi"})
        assert r.status_code == 200
        body = r.json()
        assert body["reply"] == "echo:hi"
        assert body["session_id"] == "new-session"

        r2 = client.post("/chat", json={"message": "again", "session_id": "abc"})
        assert r2.json()["session_id"] == "abc"
    finally:
        app.dependency_overrides.clear()


def test_chat_requires_message():
    client = TestClient(app)
    r = client.post("/chat", json={})
    assert r.status_code == 422  # validation error: message required


@pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to run live model tests"
)
def test_chat_live_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    monkeypatch.setenv("DAYLOOK_USE_MCP", "0")
    from daylook.capture_script.mock_generator import generate
    from datetime import datetime
    generate(start=datetime(2026, 7, 5, 9, 0, 0), clean=True)

    # fresh Root against the temp home
    import daylook.api.main as api
    api._root = None
    client = TestClient(app)
    r = client.post("/chat", json={"message": "Process my day and show the journal."})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"]
    assert body["session_id"]
    api._root = None


def test_journal_backlog_groups_pending_by_date(tmp_path, monkeypatch):
    """The boot-time 'you have unprocessed days' nudge needs pending counts per date."""
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    from datetime import datetime
    from daylook.capture_script.mock_generator import generate

    client = TestClient(app)
    assert client.get("/journal/backlog").json() == {"days": [], "total": 0}

    generate(start=datetime(2026, 7, 5, 9, 0, 0))
    generate(start=datetime(2026, 7, 6, 9, 0, 0))
    body = client.get("/journal/backlog").json()
    assert [d["date"] for d in body["days"]] == ["2026-07-05", "2026-07-06"]
    assert body["total"] == sum(d["events"] for d in body["days"]) > 0
