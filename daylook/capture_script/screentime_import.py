"""Import iPhone/iPad activity into the Mac journal via Screen Time (Phase 3).

**The idea, and its honest limits.** iOS/iPadOS forbid third-party apps from capturing the
screen or reading the foreground app of *other* apps — so Daylook can't run its Mac-style
capture on a phone. But when Apple's Screen Time **"Share Across Devices"** is on, the phone/
iPad's per-app usage syncs to the Mac's local ``knowledgeC.db`` (CoreDuet), **device-tagged**
via ``ZSOURCE.ZDEVICEID``. We read that here and fold the *other* devices' usage into the
journal as coarse, clearly-labelled blocks (app + duration; no screenshots, no "which reel").

**Two hard requirements, both one-time:**
  1. Screen Time → *Share Across Devices* enabled on the Mac and the phone.
  2. **Full Disk Access** granted to Daylook — ``knowledgeC.db`` is TCC-protected; without the
     grant every read is ``authorization denied``. There is no API and no way around this
     (verified). So this module is a **no-op until FDA is granted**: :func:`import_screentime`
     catches the permission error and reports ``available=False`` instead of failing.

Schema notes (from the APOLLO / mac4n6 forensics work): stream ``/app/usage`` rows live in
``ZOBJECT`` with ``ZVALUESTRING`` = app bundle id, ``ZSTARTDATE``/``ZENDDATE`` in **Mac
absolute time** (seconds since 2001-01-01), and ``ZSOURCE`` → ``ZSOURCE.ZDEVICEID``. On
macOS 13+/iOS 16+ some of this is migrating to **Biome** (harder, protobuf) — a later
enhancement; ``knowledgeC.db`` is still populated on current macOS.

Run the spike (after granting Full Disk Access):
    python -m daylook.capture_script.screentime_import --probe
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Seconds between the Unix epoch (1970) and the Mac/CoreData absolute epoch (2001).
_MAC_EPOCH_OFFSET = 978307200


def knowledgec_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Knowledge" / "knowledgeC.db"


def _mac_abs_to_dt(value: float) -> datetime:
    """Mac absolute time (float seconds since 2001) -> local naive datetime (matches the
    ISO timestamps the rest of the journal uses)."""
    return datetime.fromtimestamp(value + _MAC_EPOCH_OFFSET)


def local_device_uuid() -> str | None:
    """This Mac's hardware UUID, so we can *exclude* its own rows (already captured natively).
    Best-effort via ioreg; None if it can't be determined (then we fall back to excluding only
    NULL-device rows)."""
    try:
        out = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                return line.split('"')[-2]
    except Exception:
        pass
    return None


@dataclass(frozen=True)
class UsageRow:
    device_id: str | None
    bundle_id: str
    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> int:
        return max(0, int((self.end - self.start).total_seconds()))


# A few common iOS bundle ids → friendly names; anything else falls back to a tidy guess.
_APP_NAMES = {
    "com.burbn.instagram": "Instagram", "com.zhiliaoapp.musically": "TikTok",
    "com.google.ios.youtube": "YouTube", "com.atebits.Tweetie2": "X",
    "com.reddit.Reddit": "Reddit", "com.hammerandchisel.discord": "Discord",
    "com.toyopagroup.picaboo": "Snapchat", "net.whatsapp.WhatsApp": "WhatsApp",
    "com.apple.mobilesafari": "Safari", "com.apple.MobileSMS": "Messages",
    "com.spotify.client": "Spotify", "com.google.Gmail": "Gmail",
    "com.linkedin.LinkedIn": "LinkedIn", "com.facebook.Facebook": "Facebook",
}


def friendly_app_name(bundle_id: str) -> str:
    if bundle_id in _APP_NAMES:
        return _APP_NAMES[bundle_id]
    tail = bundle_id.rsplit(".", 1)[-1] if bundle_id else "App"
    return tail.replace("-", " ").replace("_", " ").title() or "App"


class ScreenTimeUnavailable(Exception):
    """knowledgeC.db can't be read — almost always missing Full Disk Access."""


def read_app_usage(
    db_path: Path | None = None,
    exclude_device_ids: set[str] | None = None,
) -> list[UsageRow]:
    """Read ``/app/usage`` rows for devices OTHER than this Mac.

    Raises :class:`ScreenTimeUnavailable` if the DB can't be opened (the FDA case). Rows with a
    NULL device or a device id in ``exclude_device_ids`` (this Mac) are dropped — the Mac is
    already captured natively, so importing it would double-count.
    """
    path = db_path or knowledgec_path()
    exclude = set(exclude_device_ids or ())
    local = local_device_uuid()
    if local:
        exclude.add(local)
    try:
        conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    except sqlite3.OperationalError as e:
        raise ScreenTimeUnavailable(str(e)) from e
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT o.ZVALUESTRING AS bundle_id, o.ZSTARTDATE AS start, "
                "       o.ZENDDATE AS end, s.ZDEVICEID AS device_id "
                "FROM ZOBJECT o LEFT JOIN ZSOURCE s ON o.ZSOURCE = s.Z_PK "
                "WHERE o.ZSTREAMNAME = '/app/usage' AND o.ZVALUESTRING IS NOT NULL "
                "  AND o.ZSTARTDATE IS NOT NULL AND o.ZENDDATE IS NOT NULL "
                "ORDER BY o.ZSTARTDATE"
            ).fetchall()
        except sqlite3.OperationalError as e:
            # Schema drift across macOS versions — surface as unavailable, don't crash.
            raise ScreenTimeUnavailable(f"unexpected knowledgeC schema: {e}") from e
    finally:
        conn.close()

    out: list[UsageRow] = []
    for r in rows:
        dev = r["device_id"]
        if dev is None or dev in exclude:
            continue  # this Mac (or unattributed) — already captured natively
        try:
            out.append(UsageRow(
                device_id=dev, bundle_id=r["bundle_id"],
                start=_mac_abs_to_dt(r["start"]), end=_mac_abs_to_dt(r["end"]),
            ))
        except Exception:
            continue
    return out


def _device_label(device_id: str, seen: dict[str, str]) -> str:
    """A stable, human-ish source tag per device. Without reliable model metadata in
    knowledgeC we can't always tell iPhone from iPad, so distinct devices become
    'iphone', 'iphone-2', … — good enough to badge and to replace idempotently."""
    if device_id not in seen:
        seen[device_id] = "iphone" if not seen else f"iphone-{len(seen) + 1}"
    return seen[device_id]


def usage_to_blocks_by_day(rows: list[UsageRow], min_seconds: int = 60) -> dict[str, list[dict]]:
    """Group usage rows into per-day, per-device, merged app blocks (drops sub-``min_seconds``
    noise). Blocks carry ``source`` (device tag) and category 'other' — we report time, we
    don't judge it (consistent with the rest of the journal)."""
    from daylook.agents.analyst import merge_blocks

    seen: dict[str, str] = {}
    raw: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r.duration_seconds < min_seconds:
            continue
        source = _device_label(r.device_id, seen)
        day = r.start.date().isoformat()
        name = friendly_app_name(r.bundle_id)
        raw.setdefault((day, source), []).append({
            "start": r.start.isoformat(timespec="seconds"),
            "end": r.end.isoformat(timespec="seconds"),
            "duration_seconds": r.duration_seconds,
            "label": f"{name}",
            "category": "other",
            "app_name": name,
            "detail": "",
            "source_event_ids": [],
        })

    by_day: dict[str, list[dict]] = {}
    for (day, source), blocks in raw.items():
        blocks.sort(key=lambda b: b["start"])
        for b in merge_blocks(blocks):
            b["source"] = source
            b["label"] = f"📱 {b['label']}"
            by_day.setdefault(day, []).append(b)
    for day in by_day:
        by_day[day].sort(key=lambda b: b["start"])
    return by_day


def import_screentime(memory, db_path: Path | None = None) -> dict:
    """Read cross-device Screen Time usage and merge it into the journal, per day.

    Never raises: returns a summary dict ``{available, imported, days, error}``. ``available``
    is False (with no error surfaced to the user) when Full Disk Access hasn't been granted —
    the hourly hook calls this unconditionally and it simply does nothing until then.
    """
    try:
        rows = read_app_usage(db_path)
    except ScreenTimeUnavailable as e:
        return {"available": False, "imported": 0, "days": [], "error": str(e)}
    by_day = usage_to_blocks_by_day(rows)
    imported = 0
    for day, blocks in by_day.items():
        imported += memory.store.replace_imported_blocks(day, blocks)
    return {"available": True, "imported": imported, "days": sorted(by_day)}


def probe(db_path: Path | None = None) -> int:
    """Human-readable feasibility check — the Phase 3 spike. Prints the device breakdown and a
    sample so we can confirm iPhone/iPad rows actually synced in. Returns a process exit code."""
    path = db_path or knowledgec_path()
    print(f"knowledgeC: {path}")
    print(f"this Mac's hardware UUID: {local_device_uuid()}")
    try:
        rows = read_app_usage(path)
    except ScreenTimeUnavailable as e:
        print(f"\nNOT READABLE — {e}")
        print("Grant Daylook (or your terminal) Full Disk Access in System Settings → "
              "Privacy & Security → Full Disk Access, and enable Screen Time → Share Across "
              "Devices, then re-run.")
        return 1
    if not rows:
        print("\nReadable, but no NON-Mac usage rows found. Either Share Across Devices is off, "
              "the phone hasn't synced yet, or this macOS keeps it in Biome (not knowledgeC).")
        return 2
    by_dev: dict[str, int] = {}
    for r in rows:
        by_dev[r.device_id] = by_dev.get(r.device_id, 0) + r.duration_seconds
    print(f"\nFound {len(rows)} non-Mac usage rows across {len(by_dev)} device(s):")
    for dev, secs in sorted(by_dev.items(), key=lambda x: -x[1]):
        print(f"  device {dev[:12]}…  {secs // 60} min total")
    print("\nSample (most recent 10):")
    for r in sorted(rows, key=lambda r: r.start)[-10:]:
        print(f"  {r.start:%Y-%m-%d %H:%M}  {friendly_app_name(r.bundle_id):<14} "
              f"{r.duration_seconds // 60}m  [{r.device_id[:8]}…]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Import iPhone/iPad Screen Time usage (Phase 3).")
    ap.add_argument("--probe", action="store_true",
                    help="feasibility check: print cross-device usage found (needs Full Disk Access)")
    args = ap.parse_args()
    if args.probe:
        return probe()
    # Non-probe: import into the real journal.
    from daylook.agents.memory import MemoryAgent
    mem = MemoryAgent()
    try:
        result = import_screentime(mem)
    finally:
        mem.close()
    print(result)
    return 0 if result["available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
