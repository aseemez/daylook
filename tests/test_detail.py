"""The `detail` field: Analyst grouping -> derived blocks -> merge -> SQLite round-trip.

Details are the journal's substance (the fix for "the log was generic — 'used X'"): the
Analyst writes 1-2 concrete sentences per block, and every later stage must carry them
without loss — including block merging and the storage migration on a pre-detail database.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from daylook.agents.analyst import _Group, _Grouping, _merge_two, derive_blocks, summarize_day
from daylook.events import CaptureEvent, EventBatch
from daylook.storage.sqlite import Store


def _batch(specs: list[tuple[str, int]]) -> EventBatch:
    base = datetime(2026, 7, 5, 9, 0, 0)
    events = [
        CaptureEvent(
            event_id=f"evt_{i:04d}",
            timestamp=(base + timedelta(minutes=off)).isoformat(timespec="seconds"),
            app_name=app,
            window_title=f"{app} window",
        )
        for i, (app, off) in enumerate(specs, start=1)
    ]
    return EventBatch(batch_id="batch_test", events=tuple(events))


def test_detail_flows_from_grouping_into_derived_blocks():
    batch = _batch([("Safari", 0), ("Safari", 5)])
    grouping = _Grouping(blocks=[
        _Group(label="Reading Gumclaw docs", category="research",
               detail="Read the 'How Gumclaw Works' page, mostly the pricing section.",
               event_ids=["evt_0001", "evt_0002"]),
    ])
    blocks = derive_blocks(batch, grouping).blocks
    assert blocks[0].detail.startswith("Read the 'How Gumclaw Works'")


def test_old_cached_grouping_without_detail_still_validates():
    g = _Group(label="Coding", category="coding", event_ids=["evt_0001"])
    assert g.detail == ""


def test_tidy_detail_caps_sentences_and_dedupes_repeats():
    from daylook.agents.analyst import tidy_detail
    # Merge-verbosity (the user's complaint): 4 sentences, two of them repeats, trimmed to 2.
    noisy = ("Left the video paused on a title card. Screen didn't change the whole time. "
             "Left the video paused on a title card. Screen didn't change the whole time.")
    out = tidy_detail(noisy)
    assert out == "Left the video paused on a title card. Screen didn't change the whole time."

    # A single specific sentence is left intact.
    one = "Watched 'You need to read less code' by Theo and scrolled the comments."
    assert tidy_detail(one) == one

    # Very long text is hard-capped with a single ellipsis; re-tidying never compounds it.
    capped = tidy_detail("word " * 200)
    assert len(capped) <= 261 and capped.endswith("…") and "……" not in capped
    assert "……" not in tidy_detail(capped)

    # Real multi-sentence details (the actual merge case) ARE fully idempotent.
    real = ("Watched Matt Pocock's AI coding video. Left it paused on a title card for a while.")
    assert tidy_detail(real) == tidy_detail(tidy_detail(real)) == real
    assert tidy_detail("") == ""


def test_merge_keeps_both_details_in_order_and_caps_length():
    a = {"start": "s", "end": "m", "duration_seconds": 600, "label": "A", "category": "coding",
         "detail": "First thing.", "app_name": "Cursor", "source_event_ids": ["e1"]}
    b = {"start": "m", "end": "e", "duration_seconds": 60, "label": "B", "category": "coding",
         "detail": "Second thing.", "app_name": "Cursor", "source_event_ids": ["e2"]}
    merged = _merge_two(a, b)
    assert merged["detail"] == "First thing. Second thing."
    long = dict(b, detail="x" * 1000)
    assert len(_merge_two(a, long)["detail"]) <= 600


def test_merge_label_and_detail_come_from_the_same_block():
    # The label is taken from the LONGER block; the detail must lead with THAT block's detail
    # too, so a merge of two different-topic same-app blocks never shows one topic's label over
    # the other topic's description (the "one topic, different description" mismatch). Here the
    # longer block is the SECOND one, so chronological-first would have mismatched.
    short_first = {"start": "s", "end": "m", "duration_seconds": 60, "label": "Wire bending",
                   "category": "research", "detail": "About wire bending curves.",
                   "app_name": "Safari", "source_event_ids": ["e1"]}
    long_second = {"start": "m", "end": "e", "duration_seconds": 900, "label": "FP8 quantization",
                   "category": "research", "detail": "About FP8 to INT8 quantization.",
                   "app_name": "Safari", "source_event_ids": ["e2"]}
    merged = _merge_two(short_first, long_second)
    assert merged["label"] == "FP8 quantization"
    assert merged["detail"].startswith("About FP8 to INT8 quantization.")


def test_summarize_day_tolerates_blocks_without_detail():
    legacy = {"start": "2026-07-05T09:00:00", "end": "2026-07-05T09:30:00",
              "duration_seconds": 1800, "label": "Work", "category": "coding",
              "app_name": "Cursor", "source_event_ids": ["e1"]}
    day = summarize_day("2026-07-05", [legacy])
    assert day["blocks"][0]["detail"] == ""


def test_store_roundtrips_detail_and_migrates_old_dbs(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    db = tmp_path / "j.sqlite"

    # Simulate a pre-detail database: create the old shape by hand.
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE days (date TEXT PRIMARY KEY, writeup TEXT NOT NULL, created_at TIMESTAMP NOT NULL);"
        "CREATE TABLE blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,"
        " start TIMESTAMP NOT NULL, end TIMESTAMP NOT NULL, label TEXT NOT NULL,"
        " app_name TEXT NOT NULL, category TEXT, duration_seconds INTEGER,"
        " source_event_ids TEXT NOT NULL, manual INTEGER NOT NULL DEFAULT 0);"
    )
    conn.execute(
        "INSERT INTO blocks(date, start, end, label, app_name, category, duration_seconds,"
        " source_event_ids) VALUES ('2026-07-01','a','b','Old row','App','coding',60,'[]')"
    )
    conn.commit()
    conn.close()

    store = Store(db)  # migration runs here
    assert store.get_blocks("2026-07-01")[0]["detail"] == ""

    store.save_day("2026-07-05", "writeup", [{
        "start": "2026-07-05T09:00:00", "end": "2026-07-05T10:00:00",
        "duration_seconds": 3600, "label": "Deep dive", "category": "research",
        "detail": "Compared JWT rotation approaches on MDN and Stack Overflow.",
        "app_name": "Safari", "source_event_ids": ["e1"],
    }])
    got = store.get_blocks("2026-07-05")[0]
    assert got["detail"].startswith("Compared JWT rotation")
    store.close()
