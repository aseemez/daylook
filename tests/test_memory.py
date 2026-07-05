"""Tests for the Memory Agent + SQLite store (build step 5).

Hermetic: exercises the full persist/read cycle against a temp database, plus the
anti-hallucination-relevant round-trip of ``source_event_ids``. The LLM answer path is
covered opt-in in ``test_memory_llm.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contextline.agents.memory import MemoryAgent
from contextline.storage.sqlite import Store

FIXTURE = Path(__file__).parent / "fixtures" / "sample_journal.json"


@pytest.fixture
def sample():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def mem(tmp_path):
    agent = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    yield agent
    agent.close()


def test_persist_then_get_journal(mem, sample):
    date = mem.persist_day(sample["day"], sample["writeup"])
    assert date == "2026-07-05"
    row = mem.get_journal("2026-07-05")
    assert row is not None
    assert row["writeup"] == sample["writeup"]        # eval criterion 3: writeup matches
    assert row["created_at"]


def test_blocks_round_trip_source_event_ids(mem, sample):
    mem.persist_day(sample["day"], sample["writeup"])
    blocks = mem.get_blocks("2026-07-05")
    assert len(blocks) == 2
    # source_event_ids survives as a real list (JSON round-trip), not a string
    first = blocks[0]
    assert first["source_event_ids"] == ["evt_0005", "evt_0006", "evt_0007", "evt_0008"]
    assert isinstance(first["source_event_ids"], list)


def test_persist_is_idempotent(mem, sample):
    mem.persist_day(sample["day"], sample["writeup"])
    mem.persist_day(sample["day"], sample["writeup"])  # again
    assert mem.available_dates() == ["2026-07-05"]     # one day row, not two
    assert len(mem.get_blocks("2026-07-05")) == 2       # blocks replaced, not duplicated


def test_time_aggregations(mem, sample):
    mem.persist_day(sample["day"], sample["writeup"])
    by_cat = mem.store.time_by_category("2026-07-05")
    assert by_cat == {"coding": 1680, "review": 840}
    by_app = mem.store.time_by_app("2026-07-05")
    assert by_app["Cursor"] == 1680


def test_find_blocks_by_app_across_days(mem, sample):
    mem.persist_day(sample["day"], sample["writeup"])
    hits = mem.store.find_blocks_by_app("Cursor")
    assert len(hits) == 1
    assert hits[0]["label"].startswith("Debugging")


def test_missing_day_returns_none(mem):
    assert mem.get_journal("1999-01-01") is None
    assert mem.get_blocks("1999-01-01") == []


def test_updated_writeup_replaces_old(mem, sample):
    mem.persist_day(sample["day"], sample["writeup"])
    mem.persist_day(sample["day"], "# Updated writeup")
    assert mem.get_journal("2026-07-05")["writeup"] == "# Updated writeup"


def test_store_in_memory_works():
    s = Store(":memory:")
    s.save_day("2026-07-05", "hi", [])
    assert s.get_day("2026-07-05")["writeup"] == "hi"
    s.close()
