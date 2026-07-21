"""Shared test configuration.

The general suite forces the *plain* filesystem read path (``DAYLOOK_USE_MCP=0``) so tests
are fast and hermetic — they don't spawn an npx MCP server. The MCP read path has its own
dedicated test (``test_capture_mcp.py``) that exercises it directly.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _plain_read_path(monkeypatch):
    monkeypatch.setenv("DAYLOOK_USE_MCP", "0")
