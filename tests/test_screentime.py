"""Screen Time cross-device import (Phase 3) — tested against a synthetic knowledgeC DB.

We never touch the user's real ``knowledgeC.db`` (it's TCC-protected and personal). Instead we
build a tiny SQLite with the same shape the reader queries (``ZOBJECT`` + ``ZSOURCE``, stream
``/app/usage``, Mac-absolute timestamps) and prove: the reader drops this Mac's own rows,
extracts the other devices', converts to day-grouped blocks, and that imported blocks coexist
with — and survive re-processing of — the Mac pipeline's blocks.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from daylook.capture_script import screentime_import as st
from daylook.storage.sqlite import Store

_MAC_EPOCH_OFFSET = 978307200
MAC_UUID = "MAC-LOCAL-UUID"
IPHONE_UUID = "IPHONE-UUID-1"


def _mac_abs(y, mo, d, h, mi) -> float:
    return datetime(y, mo, d, h, mi, 0).timestamp() - _MAC_EPOCH_OFFSET


def _build_knowledgec(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE ZSOURCE (Z_PK INTEGER PRIMARY KEY, ZDEVICEID TEXT);"
        "CREATE TABLE ZOBJECT (Z_PK INTEGER PRIMARY KEY, ZSTREAMNAME TEXT, ZVALUESTRING TEXT,"
        " ZSTARTDATE REAL, ZENDDATE REAL, ZSOURCE INTEGER);"
    )
    conn.execute("INSERT INTO ZSOURCE(Z_PK, ZDEVICEID) VALUES (1, ?)", (MAC_UUID,))
    conn.execute("INSERT INTO ZSOURCE(Z_PK, ZDEVICEID) VALUES (2, ?)", (IPHONE_UUID,))
    rows = [
        # iPhone: Instagram 25m, YouTube 40m, TikTok 30s (sub-minute → dropped in blocks).
        ("/app/usage", "com.burbn.instagram", _mac_abs(2026, 7, 25, 10, 0), _mac_abs(2026, 7, 25, 10, 25), 2),
        ("/app/usage", "com.google.ios.youtube", _mac_abs(2026, 7, 25, 11, 0), _mac_abs(2026, 7, 25, 11, 40), 2),
        ("/app/usage", "com.zhiliaoapp.musically", _mac_abs(2026, 7, 25, 12, 0), _mac_abs(2026, 7, 25, 12, 0) + 30, 2),
        # This Mac's own usage — must be EXCLUDED (already captured natively).
        ("/app/usage", "com.apple.dt.Xcode", _mac_abs(2026, 7, 25, 9, 0), _mac_abs(2026, 7, 25, 10, 0), 1),
    ]
    conn.executemany(
        "INSERT INTO ZOBJECT(ZSTREAMNAME, ZVALUESTRING, ZSTARTDATE, ZENDDATE, ZSOURCE) "
        "VALUES (?, ?, ?, ?, ?)", rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def kc(tmp_path, monkeypatch):
    path = tmp_path / "knowledgeC.db"
    _build_knowledgec(path)
    monkeypatch.setattr(st, "local_device_uuid", lambda: MAC_UUID)
    return path


def test_reader_excludes_this_mac_and_reads_the_phone(kc):
    rows = st.read_app_usage(kc)
    assert {r.bundle_id for r in rows} == {
        "com.burbn.instagram", "com.google.ios.youtube", "com.zhiliaoapp.musically",
    }
    assert all(r.device_id == IPHONE_UUID for r in rows)  # Mac's Xcode row excluded
    inst = next(r for r in rows if r.bundle_id == "com.burbn.instagram")
    assert inst.duration_seconds == 25 * 60


def test_usage_becomes_day_grouped_badged_blocks(kc):
    by_day = st.usage_to_blocks_by_day(st.read_app_usage(kc))
    assert list(by_day) == ["2026-07-25"]
    blocks = by_day["2026-07-25"]
    labels = [b["label"] for b in blocks]
    assert any("Instagram" in l and l.startswith("📱") for l in labels)
    assert any("YouTube" in l for l in labels)
    assert all("TikTok" not in l for l in labels)          # 30s row dropped as noise
    assert all(b["source"] == "iphone" for b in blocks)
    assert all(b["category"] == "other" for b in blocks)   # we report, we don't judge


def test_import_coexists_with_and_survives_mac_reprocessing(kc, tmp_path, monkeypatch):
    monkeypatch.setenv("DAYLOOK_HOME", str(tmp_path))
    store = Store(tmp_path / "j.sqlite")

    class _Mem:  # minimal stand-in for MemoryAgent (import only touches .store)
        pass
    mem = _Mem(); mem.store = store

    res = st.import_screentime(mem, db_path=kc)
    assert res["available"] and res["imported"] >= 2
    got = store.get_blocks("2026-07-25")
    assert got and all(b["source"] == "iphone" for b in got)

    # The Mac pipeline saves its own day → imported phone blocks must NOT be wiped.
    store.save_day("2026-07-25", "mac writeup", [{
        "start": "2026-07-25T14:00:00", "end": "2026-07-25T15:00:00", "duration_seconds": 3600,
        "label": "Coding", "category": "coding", "app_name": "Cursor", "source_event_ids": ["e1"],
    }])
    after = store.get_blocks("2026-07-25")
    sources = {b["source"] for b in after}
    assert sources == {"mac", "iphone"}

    # Re-importing replaces (idempotent) — no duplicate phone blocks pile up.
    st.import_screentime(mem, db_path=kc)
    phone = [b for b in store.get_blocks("2026-07-25") if b["source"] == "iphone"]
    assert len(phone) == len([b for b in got])
    store.close()


def test_unavailable_db_is_a_quiet_no_op(tmp_path):
    # A missing/denied DB (the pre-Full-Disk-Access reality) must report unavailable, not raise.
    class _Mem:
        pass
    mem = _Mem(); mem.store = Store(tmp_path / "j.sqlite")
    res = st.import_screentime(mem, db_path=tmp_path / "nope.db")
    assert res["available"] is False and res["imported"] == 0
    mem.store.close()
