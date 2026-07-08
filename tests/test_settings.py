"""Tests for user settings + the env translation the launcher/app relies on.

Hermetic: each test points CONTEXTLINE_HOME at a tmp dir and neutralizes the three model-routing
env vars via monkeypatch so apply_to_env's direct os.environ writes are reverted at teardown.
"""

from __future__ import annotations

import json

import pytest

from contextline import settings


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    # snapshot + auto-restore whatever apply_to_env touches
    for var in ("GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(var, raising=False)


def test_defaults_when_no_file():
    data = settings.load()
    assert data["api_key_mode"] == "relay"
    assert data["invite_token"] == ""
    assert settings.is_configured(data) is False


def test_save_roundtrip_and_configured():
    settings.save({"invite_token": "cl_abc123"})
    data = settings.load()
    assert data["invite_token"] == "cl_abc123"
    assert settings.is_configured(data) is True
    # persisted to disk as JSON
    on_disk = json.loads(settings.settings_path().read_text())
    assert on_disk["invite_token"] == "cl_abc123"


def test_save_ignores_unknown_keys():
    settings.save({"invite_token": "cl_x", "totally_unknown": "nope"})
    assert "totally_unknown" not in settings.load()


def test_corrupt_file_falls_back_to_defaults():
    settings.settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings.settings_path().write_text("{ not json")
    data = settings.load()  # must not raise
    assert data["api_key_mode"] == "relay"


def test_apply_to_env_relay_mode(monkeypatch):
    import os
    applied = settings.apply_to_env(
        {"api_key_mode": "relay", "invite_token": "cl_tok", "relay_url": "https://relay.example"}
    )
    assert applied is True
    assert os.environ["GOOGLE_API_KEY"] == "cl_tok"
    assert os.environ["GOOGLE_GEMINI_BASE_URL"] == "https://relay.example"
    assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "FALSE"
    assert os.environ["CONTEXTLINE_USE_MCP"] == "0"


def test_apply_to_env_own_key_mode_no_base_url():
    import os
    os.environ["GOOGLE_GEMINI_BASE_URL"] = "https://leftover"  # reverted by monkeypatch fixture
    settings.apply_to_env({"api_key_mode": "own_key", "invite_token": "AIzaOwnKey"})
    assert os.environ["GOOGLE_API_KEY"] == "AIzaOwnKey"
    assert "GOOGLE_GEMINI_BASE_URL" not in os.environ  # own key talks straight to Google


def test_apply_to_env_noop_without_token():
    assert settings.apply_to_env({"invite_token": ""}) is False
