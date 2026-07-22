"""Tests for the editable calendar: manual block CRUD in the store and over the API.

The calendar shows what capture recorded, but the user can correct/augment it by hand (add an
offline meeting, fix a label). Two invariants matter here:
  * manual blocks survive re-processing of the same day (``save_day`` only clears derived ones);
  * the API derives ISO timestamps + duration from ``HH:MM`` and rejects a non-positive span.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from daylook.agents.memory import MemoryAgent
from daylook.api.main import app, get_memory
from daylook.storage.sqlite import Store


# --- store level ---------------------------------------------------------------------

def test_add_get_update_delete_block():
    s = Store(":memory:")
    bid = s.add_block(
        "2026-07-05", "2026-07-05T09:00:00", "2026-07-05T09:30:00",
        "Offline planning", "", "planning", 1800, manual=True,
    )
    got = s.get_block(bid)
    assert got["label"] == "Offline planning"
    assert got["manual"] is True
    assert got["source_event_ids"] == []          # manual blocks trace to the user, not capture

    assert s.update_block(bid, label="Sprint planning", category="meeting") is True
    got = s.get_block(bid)
    assert got["label"] == "Sprint planning" and got["category"] == "meeting"

    assert s.delete_block(bid) is True
    assert s.get_block(bid) is None
    assert s.delete_block(bid) is False           # already gone


def test_save_day_preserves_manual_blocks():
    s = Store(":memory:")
    s.save_day("2026-07-05", "v1", [{
        "start": "2026-07-05T10:00:00", "end": "2026-07-05T10:20:00",
        "label": "Captured work", "app_name": "Cursor", "category": "coding",
        "duration_seconds": 1200, "source_event_ids": ["e1"],
    }])
    s.add_block("2026-07-05", "2026-07-05T12:00:00", "2026-07-05T12:30:00",
                "Lunch with team", "", "break", 1800, manual=True)

    # Re-processing the day replaces derived blocks but must keep the hand-added one.
    s.save_day("2026-07-05", "v2", [{
        "start": "2026-07-05T10:00:00", "end": "2026-07-05T10:40:00",
        "label": "Captured work (reprocessed)", "app_name": "Cursor", "category": "coding",
        "duration_seconds": 2400, "source_event_ids": ["e1", "e2"],
    }])
    labels = [b["label"] for b in s.get_blocks("2026-07-05")]
    assert "Lunch with team" in labels
    assert "Captured work (reprocessed)" in labels
    assert "Captured work" not in labels          # the old derived block was replaced


def test_dates_with_content_includes_manual_only_day():
    s = Store(":memory:")
    s.add_block("2026-01-02", "2026-01-02T09:00:00", "2026-01-02T09:30:00",
                "Solo day", "", "other", 1800, manual=True)
    assert "2026-01-02" in s.dates_with_content()   # no 'days' row, yet it still lists


# --- API level -----------------------------------------------------------------------

@pytest.fixture
def client():
    mem = MemoryAgent(store=Store(":memory:"))
    app.dependency_overrides[get_memory] = lambda: mem
    try:
        yield TestClient(app), mem
    finally:
        app.dependency_overrides.clear()


def test_api_add_edit_delete_block(client):
    c, mem = client
    date = "2026-07-05"

    r = c.post(f"/journal/{date}/blocks",
               json={"start": "09:00", "end": "09:45", "label": "Deep work", "category": "coding"})
    assert r.status_code == 200
    block = r.json()["block"]
    assert block["manual"] is True
    assert block["duration_seconds"] == 45 * 60
    assert block["start"].endswith("T09:00:00")
    bid = block["id"]

    # it shows up in the day payload and the sidebar date list
    day = c.get(f"/journal/{date}").json()
    assert any(b["id"] == bid for b in day["blocks"])
    assert date in c.get("/journal/dates").json()["dates"]

    r = c.patch(f"/journal/{date}/blocks/{bid}", json={"label": "Focused coding", "end": "10:15"})
    assert r.status_code == 200
    edited = r.json()["block"]
    assert edited["label"] == "Focused coding"
    assert edited["duration_seconds"] == 75 * 60   # times re-derived together

    r = c.delete(f"/journal/{date}/blocks/{bid}")
    assert r.status_code == 200
    assert c.get(f"/journal/{date}").json()["blocks"] == []


def test_api_rejects_bad_span(client):
    c, _ = client
    r = c.post("/journal/2026-07-05/blocks",
               json={"start": "10:00", "end": "10:00", "label": "Zero"})
    assert r.status_code == 400


def test_api_edit_wrong_day_is_404(client):
    c, _ = client
    bid = c.post("/journal/2026-07-05/blocks",
                 json={"start": "09:00", "end": "09:30", "label": "x"}).json()["block"]["id"]
    r = c.patch(f"/journal/2026-07-06/blocks/{bid}", json={"label": "nope"})
    assert r.status_code == 404


def test_api_reclassify_band(client):
    """Correcting a band's type sets category on all its blocks at once."""
    c, _ = client
    date = "2026-07-05"
    ids = [
        c.post(f"/journal/{date}/blocks", json={"start": "09:00", "end": "09:20", "label": "a", "category": "social"}).json()["block"]["id"],
        c.post(f"/journal/{date}/blocks", json={"start": "09:20", "end": "09:40", "label": "b", "category": "social"}).json()["block"]["id"],
    ]
    r = c.post(f"/journal/{date}/reclassify", json={"ids": ids, "category": "coding"})
    assert r.status_code == 200 and r.json()["updated"] == 2
    cats = {b["category"] for b in c.get(f"/journal/{date}").json()["blocks"]}
    assert cats == {"coding"}   # both blocks moved off "social"
