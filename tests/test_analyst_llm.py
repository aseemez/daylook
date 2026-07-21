"""Live vision test for the Analyst (build step 3) — opt-in.

Makes real Vertex AI calls, so it's skipped unless ``RUN_LLM_TESTS=1`` to keep the default
suite free and offline. Verifies the full path: a real batch of screenshots comes back as
blocks whose source_event_ids are all real, and that a second call is served from cache.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from daylook.agents.analyst import analyze_batch
from daylook.agents.capture import batch_events
from daylook.capture_script.mock_generator import generate

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to run live model tests"
)


def test_analyst_produces_valid_blocks_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))  # isolate cache + data
    pending = tmp_path / "pending"
    events = generate(pending=pending, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    small = events[:3]
    batch = batch_events(small, max_events=100)[0]

    analysis = analyze_batch(batch, pending_dir=pending)

    assert analysis.blocks, "expected at least one block"
    valid_ids = {e.event_id for e in small}
    for b in analysis.blocks:
        assert b.source_event_ids, "every block traces to real events"
        assert set(b.source_event_ids) <= valid_ids, "no hallucinated event ids"
        assert b.app_name in {e.app_name for e in small}, "no hallucinated app"
        assert b.duration_seconds >= 0

    # full coverage
    covered = {eid for b in analysis.blocks for eid in b.source_event_ids}
    assert covered == valid_ids

    # second call is a cache hit (fast, no error) and identical
    again = analyze_batch(batch, pending_dir=pending)
    assert [b.to_dict() for b in again.blocks] == [b.to_dict() for b in analysis.blocks]
