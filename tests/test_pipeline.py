"""Hermetic test of the processing pipeline (build step 6 support).

Injects stub analyze/journal functions so the whole orchestration — capture, aggregate,
journal, persist, move-to-processed, and refinement threading — is exercised without any
model calls.
"""

from __future__ import annotations

from datetime import datetime

from daylook.agents.analyst import BatchAnalysis, LabeledBlock
from daylook.agents.memory import MemoryAgent
from daylook.capture_script.mock_generator import generate
from daylook.pipeline import Pipeline


def _fake_analyze(batches, pending_dir=None, use_cache=True):
    analyses = []
    for b in batches:
        block = LabeledBlock(
            start=b.events[0].timestamp,
            end=b.events[-1].timestamp,
            duration_seconds=600,
            label=f"Work in {b.events[0].app_name}",
            category="coding",
            app_name=b.events[0].app_name,
            source_event_ids=list(b.event_ids),
        )
        analyses.append(BatchAnalysis(batch_id=b.batch_id, date=b.date, blocks=[block]))
    return analyses


def test_pipeline_processes_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))  # pending + processed under tmp
    from daylook import config
    pending = config.pending_dir()
    generate(pending=pending, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)

    captured_refinements = {}

    def fake_journal(day, refinements=None, use_cache=True):
        captured_refinements["value"] = refinements
        return f"# Journal — {day['date']}\n\n{len(day['blocks'])} blocks."

    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    pipe = Pipeline(mem, analyze_fn=_fake_analyze, journal_fn=fake_journal)

    result = pipe.process_day(refinements=["exclude Slack from focus time"])

    # produced a dated result with blocks
    assert result["date"] == "2026-07-05"
    assert result["num_events"] == 30
    # aggregate_day now de-fragments blocks (analyst.merge_blocks) so a day reads as a few
    # meaningful activities, not a per-batch log — here the stub's same-category blocks collapse
    # by app. Still must be more than one block (eval criterion 1: time-bucketed, not prose).
    assert result["num_blocks"] >= 2
    assert result["writeup"].startswith("# Journal — 2026-07-05")

    # refinements were threaded into the journalist
    assert captured_refinements["value"] == ["exclude Slack from focus time"]

    # persisted to long-term memory (eval criterion 3 shape)
    row = mem.get_journal("2026-07-05")
    assert row is not None and row["writeup"] == result["writeup"]

    # events moved out of pending into processed
    assert not list(pending.glob("*.json"))
    assert list(config.processed_dir().glob("*.json"))
    mem.close()


def test_pipeline_handles_empty_pending(tmp_path):
    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    pipe = Pipeline(mem, analyze_fn=_fake_analyze, journal_fn=lambda *a, **k: "x")
    result = pipe.process_day(pending_dir=tmp_path / "empty")
    assert result["num_blocks"] == 0
    assert "No pending" in result["writeup"]
    assert result["days"] == []
    mem.close()


def test_multi_day_backlog_files_each_day_separately(tmp_path, monkeypatch):
    """Three days piled up in pending (app closed / updated mid-week) must become three
    journals under their own dates — never one mashed day that overwrites a real journal."""
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    from daylook import config
    pending = config.pending_dir()
    generate(pending=pending, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    generate(pending=pending, start=datetime(2026, 7, 6, 9, 0, 0), clean=False)
    generate(pending=pending, start=datetime(2026, 7, 7, 9, 0, 0), clean=False)

    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    pipe = Pipeline(mem, analyze_fn=_fake_analyze, journal_fn=lambda d, **k: f"J {d['date']}")
    result = pipe.process_day()

    assert [d["date"] for d in result["days"]] == ["2026-07-05", "2026-07-06", "2026-07-07"]
    # top-level result describes the most recent day (what the user usually asked about)
    assert result["date"] == "2026-07-07"
    for date in ("2026-07-05", "2026-07-06", "2026-07-07"):
        row = mem.get_journal(date)
        assert row is not None and row["writeup"] == f"J {date}"
        assert mem.get_blocks(date), f"no blocks stored for {date}"
    # every block landed under the date its events belong to
    for date in ("2026-07-05", "2026-07-06", "2026-07-07"):
        for b in mem.get_blocks(date):
            assert b["start"].startswith(date)
    assert not list(pending.glob("*.json"))
    mem.close()


def test_second_run_same_day_keeps_morning_blocks(tmp_path, monkeypatch):
    """Processing at noon then again at night must ADD the afternoon to the day's journal,
    not replace the morning (the noon run already moved its events out of pending)."""
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    from daylook import config
    pending = config.pending_dir()

    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    pipe = Pipeline(mem, analyze_fn=_fake_analyze, journal_fn=lambda d, **k: f"{len(d['blocks'])} blocks")

    generate(pending=pending, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    first = pipe.process_day()
    morning_ids = {eid for b in mem.get_blocks("2026-07-05") for eid in b["source_event_ids"]}
    assert morning_ids

    generate(pending=pending, start=datetime(2026, 7, 5, 14, 0, 0), clean=False)
    second = pipe.process_day()

    stored_ids = {eid for b in mem.get_blocks("2026-07-05") for eid in b["source_event_ids"]}
    assert morning_ids <= stored_ids, "morning events vanished from the day on the second run"
    assert len(stored_ids) > len(morning_ids), "afternoon events were not added"
    assert second["date"] == "2026-07-05"
    # totals reflect the whole day, not just the second run
    assert second["day"]["total_tracked_seconds"] >= first["day"]["total_tracked_seconds"]
    mem.close()


def test_manual_blocks_survive_reprocessing(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    from daylook import config
    pending = config.pending_dir()

    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    mem.add_block(
        date="2026-07-05", start="2026-07-05T08:00:00", end="2026-07-05T08:30:00",
        label="Offline standup", app_name="", category="meeting", duration_seconds=1800,
    )
    pipe = Pipeline(mem, analyze_fn=_fake_analyze, journal_fn=lambda d, **k: "x")
    generate(pending=pending, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    pipe.process_day()

    manual = [b for b in mem.get_blocks("2026-07-05") if b["manual"]]
    assert len(manual) == 1 and manual[0]["label"] == "Offline standup"
    mem.close()
