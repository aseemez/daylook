"""Tests for the FastAPI wrapper (build step 9).

Hermetic: ``/health`` runs directly; ``/chat`` uses a fake Root injected via FastAPI's
dependency override, so no model calls. A live end-to-end API test is opt-in below.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from contextline.api.main import app, get_root


class _FakeRoot:
    async def chat(self, message: str, session_id: str | None = None):
        return f"echo:{message}", (session_id or "new-session")


def test_health(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "model" in body
    assert body["pending_events"] in (0, None)


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
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    monkeypatch.setenv("CONTEXTLINE_USE_MCP", "0")
    from contextline.capture_script.mock_generator import generate
    from datetime import datetime
    generate(start=datetime(2026, 7, 5, 9, 0, 0), clean=True)

    # fresh Root against the temp home
    import contextline.api.main as api
    api._root = None
    client = TestClient(app)
    r = client.post("/chat", json={"message": "Process my day and show the journal."})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"]
    assert body["session_id"]
    api._root = None
