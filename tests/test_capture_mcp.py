"""Test the MCP filesystem read path (build step 2, intended interface).

Skipped automatically when Node/npx or the ``mcp`` package isn't available, so the suite
still passes on machines without them (the plain fallback covers those). When MCP *is*
available, we assert it reads exactly the same events as the plain reader.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from contextline.agents.capture import FilesystemEventSource
from contextline.agents.capture_mcp import MCPEventSource, mcp_available
from contextline.capture_script.mock_generator import generate

pytestmark = pytest.mark.skipif(not mcp_available(), reason="npx/mcp not available")


def test_mcp_reads_same_events_as_plain(tmp_path):
    generate(pending=tmp_path, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)

    plain = FilesystemEventSource(tmp_path).list_events()
    mcp = MCPEventSource(tmp_path).list_events()

    assert len(plain) == 30
    assert [e.to_dict() for e in plain] == [e.to_dict() for e in mcp]


def test_mcp_empty_dir_returns_nothing(tmp_path):
    assert MCPEventSource(tmp_path).list_events() == []
