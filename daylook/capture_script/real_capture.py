"""Real hybrid capture layer (build step 10) — macOS.

The *dumb* capture layer: it polls the frontmost application ~once a second and emits capture
events — a downscaled JPEG screenshot plus a JSON sidecar — into ``pending/``. No reasoning
lives here; that's the Capture/Analyst Agents' job. This writes the **exact same on-disk
shape** as ``mock_generator.py`` (``<event_id>.json`` + ``<event_id>.jpg``).

Design notes
------------
- **Hybrid: switches + heartbeats.** A ``switch`` event fires when the active window changes
  (and survives the debounce) — the semantic boundary. A ``heartbeat`` event fires every
  ``config.HEARTBEAT_SECONDS`` while the user stays stably in the same window — proof of
  presence that makes long focus sessions account for their full duration (switch-only
  capture credits a 2-hour session almost nothing) and gives the Analyst periodic keyframes
  of how stable work evolves. Heartbeats only start once a window is *committed* (i.e. it
  survived the debounce — the user is stably working, not flicking around).
- **Debounce.** A candidate window must persist ``DEBOUNCE_SECONDS`` before it emits, so
  rapid flicker (Cmd-Tab overshoot, transient panels) never becomes an event. The debounce
  decision is a pure state machine (:func:`advance`) so it's unit-testable without a screen.
- **Window title needs Screen Recording permission** on modern macOS; without it the title
  comes back empty and we still emit a useful app-level event. App name only needs the app to
  be frontmost (no special permission).

Consent
-------
Running this **captures your real screen**. The heavy deps (``mss``, ``pyobjc``) are imported
lazily inside :func:`run` — importing this module is always safe, but calling ``run()`` (or
``python -m daylook.capture_script.real_capture``) will start grabbing screenshots. That's
a deliberate, explicit action; nothing captures your screen just by importing.

Run it:
    python -m daylook.capture_script.real_capture            # capture until Ctrl-C
    python -m daylook.capture_script.real_capture --once     # emit one event and exit (smoke test)
    python -m daylook.capture_script.real_capture --dry-run  # print switches, write nothing
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from daylook import config
from daylook.events import CaptureEvent

# --- Tunables -------------------------------------------------------------------------
POLL_SECONDS = 1.0          # how often we sample the frontmost window
DEBOUNCE_SECONDS = 3.0      # a window must persist this long before it counts as a real switch
HEARTBEAT_SECONDS = float(config.HEARTBEAT_SECONDS)  # stable-work heartbeat cadence (0 = off)

# A window is identified by (app_name, window_title). A change in either is a context switch.
WindowKey = tuple[str, str]

# On-screen windows owned by these are system UI (menu bar, dock, overlays), not apps the user
# works in. They can appear at layer 0, so we skip them when picking the frontmost app window.
_SKIP_OWNERS = {
    "Window Server", "Dock", "Control Center", "Notification Center", "Spotlight",
    "Wallpaper", "WindowManager", "Screenshot", "Universal Control", "coreautha",
}


# --- Debounce state machine (pure, no OS calls — this is what the tests exercise) -----

@dataclass
class DebounceState:
    """Tracks the committed window and any not-yet-confirmed candidate.

    ``committed`` is the window we last emitted an event for. ``candidate``/``candidate_since``
    hold a newly-seen window that hasn't survived the debounce window yet.
    """

    committed: WindowKey | None = None
    candidate: WindowKey | None = None
    candidate_since: float = 0.0


def advance(
    state: DebounceState,
    key: WindowKey,
    now: float,
    debounce_seconds: float = DEBOUNCE_SECONDS,
) -> tuple[DebounceState, bool]:
    """Feed one poll sample into the debounce machine.

    Returns the next state and whether an event should be emitted *now* for ``key``. An event
    fires only when a window different from ``committed`` has been continuously observed for at
    least ``debounce_seconds`` — so sub-debounce flicker never emits.
    """
    if key == state.committed:
        # Back on (or still on) the committed window — cancel any pending candidate.
        return DebounceState(committed=state.committed), False

    if key != state.candidate:
        # A new candidate window — start its timer, don't emit yet.
        return DebounceState(committed=state.committed, candidate=key, candidate_since=now), False

    # Same candidate as last poll — has it now persisted long enough?
    if now - state.candidate_since >= debounce_seconds:
        return DebounceState(committed=key), True

    # Still within the debounce window — keep waiting.
    return state, False


def heartbeat_due(
    state: DebounceState, last_emit_at: float, now: float, interval: float
) -> bool:
    """Whether a stable-work heartbeat should fire on this poll tick. Pure, like :func:`advance`.

    Fires only when all of: heartbeats are enabled (interval > 0), a window is *committed*
    (survived the debounce — the user is stably on something), no candidate switch is pending
    (mid-switch screens are transitional, let the debounce resolve first), and the interval
    has elapsed since the last emission of any kind (so a fresh switch resets the clock).
    """
    return (
        interval > 0
        and state.committed is not None
        and state.candidate is None
        and now - last_emit_at >= interval
    )


# --- Capture health: can we actually SEE the screen? ----------------------------------
#
# macOS ties the Screen Recording grant to the app's code signature. An ad-hoc-signed app
# gets a NEW signature on every rebuild, so after an update macOS silently revokes the
# grant — and instead of failing, screen grabs return only the desktop wallpaper. The
# journal then honestly reports "away from computer, desktop visible" for a day the user
# worked through, which is worse than an error. Two independent detectors close that gap:
#
#   1. Permission preflight (authoritative): ask macOS directly at capture start.
#   2. Identical-frame detection (belt & braces): a SWITCH event means the frontmost window
#      changed, so the captured pixels must change too. Several consecutive switches whose
#      screenshots are byte-identical to the previous frame is impossible for real capture —
#      the frame is frozen. Heartbeats deliberately don't count: a static screen while the
#      user reads the same page for minutes is normal.
#
# Both feed ``capture_health.json`` in the Daylook home; /capture/status surfaces it and the
# UI turns it into a "can't see your screen" warning instead of a silent bad journal.

class BlindnessDetector:
    """Pure frozen-frame detector (unit-testable without a screen). Feed it the hash of every
    written frame; ``blind`` trips after ``threshold`` consecutive switch frames that were
    byte-identical to the frame before them. Any changed frame resets it — so if the user
    re-grants permission mid-run, the warning clears on the next real switch."""

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.identical_switches = 0
        self._prev_hash: str | None = None

    def observe(self, kind: str, image_hash: str) -> None:
        if self._prev_hash is not None and image_hash == self._prev_hash:
            if kind == "switch":
                self.identical_switches += 1
        elif image_hash != self._prev_hash:
            self.identical_switches = 0
        self._prev_hash = image_hash

    @property
    def blind(self) -> bool:
        return self.identical_switches >= self.threshold


def frame_signature(im: Image.Image) -> bytes:
    """A coarse fingerprint of what's ON the screen, for staleness detection.

    Deliberately NOT the byte hash used for blindness: staleness needs "did anything the
    user would notice change", not "are the files identical". The frame is shrunk to a
    small grayscale thumbnail and the top strip is cropped off (the menu bar — its clock
    repaints every minute and would keep an abandoned screen looking active forever).
    Comparison happens in :func:`frames_differ` with a tolerance, because exact equality
    is defeated by a single blinking text caret."""
    thumb = im.convert("L").resize((96, 60))
    return thumb.crop((0, 5, 96, 60)).tobytes()


def frames_differ(a: bytes | None, b: bytes, threshold: float = 2.0) -> bool:
    """Whether two frame signatures show a humanly-visible change.

    Mean absolute pixel difference over the thumbnail. Calibration: a blinking caret moves
    a couple of thumbnail pixels (MAD ≈ 0.1 — below threshold, correctly 'still stale');
    scrolling or typing a line shifts whole regions (MAD >> 2); even a small video playing
    in a corner clears the bar (correctly 'active' — watching IS activity)."""
    if a is None or len(a) != len(b):
        return True
    return sum(abs(x - y) for x, y in zip(a, b)) / len(b) >= threshold


class StaleTracker:
    """Pure staleness clock (unit-testable without a screen).

    Tracks when the screen last visibly changed: a window switch always counts as activity;
    a frame counts only if it differs (per :func:`frames_differ`) from the previous one.
    ``stale(now)`` says the screen has been frozen past the threshold — the signal to
    auto-pause so an unattended screen isn't credited as presence."""

    def __init__(self, threshold_seconds: float, now: float):
        self.threshold = threshold_seconds
        self.last_change_at = now
        self._sig: bytes | None = None

    def observe_switch(self, now: float) -> None:
        self.last_change_at = now

    def observe_frame(self, sig: bytes, now: float) -> bool:
        """Feed one frame signature; returns True if the screen visibly changed."""
        if frames_differ(self._sig, sig):
            self._sig = sig
            self.last_change_at = now
            return True
        return False

    def stale(self, now: float) -> bool:
        return self.threshold > 0 and (now - self.last_change_at) >= self.threshold


def preflight_screen_permission() -> bool | None:
    """Ask macOS whether this process may record the screen. None = can't tell (no Quartz,
    or an OS too old for the preflight API) — treated as 'assume ok', never as an error."""
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # type: ignore
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def request_screen_permission() -> None:
    """Trigger the macOS Screen Recording prompt / System Settings entry (once per app
    identity — repeat calls are a no-op, so this is safe to call on every blind start)."""
    try:
        from Quartz import CGRequestScreenCaptureAccess  # type: ignore
        CGRequestScreenCaptureAccess()
    except Exception:
        pass


def write_health(
    permission_granted: bool | None,
    detector: BlindnessDetector | None,
    running: bool,
    state: str = "recording",
) -> None:
    """Persist the current capture-vision verdict for the app/UI to read. Best-effort —
    health reporting must never be able to crash the capture loop itself.

    ``state`` is the loop's mode: "recording", "paused" (user asked), or "auto_paused"
    (stale screen — see :class:`StaleTracker`). It rides in the same file because the UI
    needs mode and blindness together to label the recording pill truthfully."""
    try:
        blind = bool(
            permission_granted is False or (detector is not None and detector.blind)
        )
        config.capture_health_path().write_text(json.dumps({
            "permission_granted": permission_granted,
            "blind": blind,
            "identical_switches": detector.identical_switches if detector else 0,
            "running": running,
            "state": state,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }))
    except OSError:
        pass


def read_health() -> dict | None:
    """The last written capture-health verdict, or None if capture never reported one."""
    try:
        return json.loads(config.capture_health_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --- OS access (lazily imported; only touched when actually capturing) ----------------

@dataclass
class _Backend:
    """Bundle of the platform capture callables, resolved once via :func:`_load_backend`."""

    frontmost: object            # () -> (app_name, window_title)
    grab_primary: object         # () -> PIL.Image.Image (full-res primary display)
    close: object = field(default=lambda: None)


def _load_backend(need_screenshots: bool = True) -> _Backend:
    """Import and wire the macOS backends (``pyobjc`` + ``mss``). Raises a clear error if missing.

    Kept out of module import so the test suite (and any text-only pipeline import) never needs
    ``mss``/``pyobjc`` installed. ``need_screenshots=False`` (used by ``--dry-run``) skips the
    ``mss`` import entirely, so switch detection can run on ``pyobjc`` alone.
    """
    try:
        from AppKit import NSWorkspace  # type: ignore
    except ImportError as e:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "Real capture needs pyobjc (AppKit). Install with: pip install pyobjc\n"
            "This is the consent-gated dependency — see CLAUDE.md / PROGRESS.md."
        ) from e

    try:
        from Quartz import (  # type: ignore
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
    except ImportError:  # pragma: no cover - Quartz missing is unusual if AppKit present
        CGWindowListCopyWindowInfo = None  # type: ignore

    def frontmost() -> WindowKey:
        # Source of truth is the window-server z-order (CGWindowList), queried fresh on every
        # call. NSWorkspace.frontmostApplication() is deliberately NOT used as the primary signal:
        # in a long-running headless script (no NSApplication run loop) it returns a STALE value —
        # it kept reporting the launching terminal even as the user switched apps, so no switches
        # were ever detected. The first on-screen layer-0 window belongs to the app actually in
        # front, and that reflects Cmd-Tab / click-to-focus immediately.
        if CGWindowListCopyWindowInfo is not None:
            windows = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly, kCGNullWindowID
            ) or []
            for w in windows:
                if w.get("kCGWindowLayer", 1) != 0:  # skip menubar/dock/control-center chrome
                    continue
                owner = (w.get("kCGWindowOwnerName") or "").strip()
                if not owner or owner in _SKIP_OWNERS:
                    continue
                return (owner, w.get("kCGWindowName", "") or "")
        # Fallback only if Quartz is somehow unavailable (may be stale, but better than nothing).
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return (app.localizedName() or "Unknown", "")

    if not need_screenshots:
        # dry-run: detect switches without ever grabbing the screen.
        def _no_grab() -> Image.Image:  # pragma: no cover - guarded by dry_run
            raise RuntimeError("screenshots disabled (dry run)")

        return _Backend(frontmost=frontmost, grab_primary=_no_grab)

    try:
        import mss  # type: ignore
    except ImportError as e:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "Real capture needs mss for screenshots. Install with: pip install mss"
        ) from e

    sct = mss.mss()

    def grab_primary() -> Image.Image:
        # monitors[1] is the primary display (monitors[0] is the "all displays" bbox).
        mon = sct.monitors[1]
        shot = sct.grab(mon)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    return _Backend(frontmost=frontmost, grab_primary=grab_primary, close=sct.close)


# --- Event writing --------------------------------------------------------------------

def _downscale(im: Image.Image) -> Image.Image:
    """Long edge <= ``IMAGE_MAX_EDGE``. Same discipline as :mod:`daylook.imaging`."""
    long_edge = max(im.size)
    if long_edge > config.IMAGE_MAX_EDGE:
        scale = config.IMAGE_MAX_EDGE / long_edge
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    return im


def make_event(
    app_name: str, window_title: str, when: datetime, kind: str = "switch"
) -> CaptureEvent:
    """Build a :class:`CaptureEvent` with a timestamp-derived, sortable, unique id.

    ``evt_<YYYYMMDDTHHMMSS>`` — the loop emits at most one event per >=1s poll tick, so
    same-second ids can't collide within a run, and the format sorts identically to its
    timestamp.
    """
    return CaptureEvent(
        event_id=f"evt_{when:%Y%m%dT%H%M%S}",
        timestamp=when.isoformat(timespec="seconds"),
        app_name=app_name,
        window_title=window_title,
        kind=kind,
    )


def write_event(event: CaptureEvent, image: Image.Image, pending: Path) -> str:
    """Persist the sidecar + downscaled JPEG — the same pair shape the mock generator writes.

    Returns the JPEG's md5 so the caller can feed the :class:`BlindnessDetector` without
    re-reading the file it just wrote."""
    event.write_sidecar(pending)
    buf = io.BytesIO()
    _downscale(image).save(buf, "JPEG", quality=config.IMAGE_JPEG_QUALITY)
    data = buf.getvalue()
    event.image_path(pending).write_bytes(data)
    return hashlib.md5(data).hexdigest()


# --- Capture loop ---------------------------------------------------------------------

def run(
    pending: Path | None = None,
    poll_seconds: float = POLL_SECONDS,
    debounce_seconds: float = DEBOUNCE_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    once: bool = False,
    dry_run: bool = False,
    max_events: int | None = None,
    duration_seconds: float | None = None,
    stop_event: "object | None" = None,
    pause_event: "object | None" = None,
    stale_pause_seconds: float | None = None,
) -> int:
    """Poll the frontmost window; emit switch events on debounced context changes and
    heartbeat events every ``heartbeat_seconds`` of stable same-window work (0 disables).

    Runs until Ctrl-C, or until any of the optional stop conditions is reached: ``once`` (one
    event), ``max_events`` (a count), ``duration_seconds`` (a wall-clock limit), or
    ``stop_event`` being set. Returns the number of events written. ``dry_run`` prints
    switches/heartbeats without writing anything (and needs no ``mss``).

    ``stop_event`` (a ``threading.Event``) is what lets the app run capture **in its own
    process on a background thread** instead of spawning a child: the app already holds the
    macOS Screen Recording grant, so recording from inside it means macOS doesn't see a second,
    separate capture client and stop re-prompting every launch. The loop waits on the event
    between polls so a stop request is honoured within one poll interval.

    ``pause_event`` (another ``threading.Event``) suspends emission while set — nothing is
    captured or written, but the loop (and the session) stays alive, so pause/resume doesn't
    tear down and re-request anything. On resume the debounce state and heartbeat clock are
    reset, so the first post-resume event is a fresh, debounced switch — never a phantom
    heartbeat for time that was paused.

    ``stale_pause_seconds`` (default ``config.STALE_PAUSE_SECONDS``, 0 disables) auto-pauses
    when the screen hasn't visibly changed for that long — the user walked away with the
    screen on, and continuing to heartbeat would credit an empty chair with presence. The
    loop keeps cheaply probing (window switch, or a changed :func:`frame_signature` checked
    at heartbeat cadence) and resumes itself within seconds of real activity. A manual
    ``pause_event`` always outranks auto-resume. Disabled in ``dry_run`` (no screenshots to
    compare).
    """
    def _stopping() -> bool:
        return stop_event is not None and stop_event.is_set()

    def _wait() -> None:
        # Interruptible wait: a threading.Event lets an in-process stop land within one
        # poll; plain time.sleep otherwise (CLI / subprocess use, stopped via Ctrl-C).
        if stop_event is not None:
            stop_event.wait(poll_seconds)
        else:
            time.sleep(poll_seconds)
    config.ensure_dirs()
    pending = Path(pending) if pending is not None else config.pending_dir()
    pending.mkdir(parents=True, exist_ok=True)

    backend = _load_backend(need_screenshots=not dry_run)
    state = DebounceState()
    written = 0
    start = time.monotonic()
    last_emit_at = start  # heartbeat clock; reset on every emission of either kind

    # Capture-vision health: preflight the Screen Recording grant, then watch for frozen
    # frames while running. If the grant is missing (classic cause: an app update changed the
    # code signature and macOS silently revoked it), say so loudly and nudge System Settings —
    # capturing wallpaper for a day while reporting "away" is the one failure this layer must
    # never be silent about.
    detector = BlindnessDetector()
    permission: bool | None = None
    was_blind = False
    if not dry_run:
        permission = preflight_screen_permission()
        if permission is False:
            print("[real_capture] WARNING: macOS Screen Recording permission is NOT granted "
                  "to this app — screenshots will show only the wallpaper. Grant it in "
                  "System Settings > Privacy & Security > Screen Recording (this resets "
                  "after app updates).")
            request_screen_permission()
        write_health(permission, detector, running=True)

    # Pause machinery. Manual pause = the pause_event; auto-pause = the StaleTracker.
    stale_seconds = (stale_pause_seconds if stale_pause_seconds is not None
                     else float(config.STALE_PAUSE_SECONDS))
    auto_pause_on = stale_seconds > 0 and not dry_run
    tracker = StaleTracker(stale_seconds, start)
    probe_interval = heartbeat_seconds if heartbeat_seconds > 0 else 12.0
    auto_paused = False
    was_paused = False
    last_probe_at = start

    stop_note = "Ctrl-C to stop" if duration_seconds is None else (
        f"auto-stops in {duration_seconds/60:.0f} min (or Ctrl-C)")
    hb_note = f", heartbeat={heartbeat_seconds:.0f}s" if heartbeat_seconds > 0 else ""
    print(f"[real_capture] watching frontmost window (poll={poll_seconds}s, "
          f"debounce={debounce_seconds}s{hb_note}){'  [DRY RUN]' if dry_run else ''}. {stop_note}.")
    try:
        while True:
            if _stopping():
                print(f"\n[real_capture] stop requested. Wrote {written} event(s) to {pending}.")
                break
            if duration_seconds is not None and time.monotonic() - start >= duration_seconds:
                print(f"\n[real_capture] reached {duration_seconds/60:.0f}-min limit. "
                      f"Wrote {written} event(s) to {pending}.")
                break

            # Manual pause: user asked to hold. Nothing is observed or written while set.
            manual_paused = pause_event is not None and pause_event.is_set()
            if manual_paused != was_paused:
                was_paused = manual_paused
                if manual_paused:
                    print("[real_capture] paused by user.")
                else:
                    # Fresh eyes on resume: re-debounce whatever is frontmost (it emits as a
                    # new switch) and reset the heartbeat + staleness clocks so paused time
                    # can never be credited as presence.
                    state = DebounceState()
                    last_emit_at = time.monotonic()
                    tracker.last_change_at = last_emit_at
                    auto_paused = False
                    print("[real_capture] resumed by user.")
                if not dry_run:
                    write_health(permission, detector, running=True,
                                 state="paused" if manual_paused else "recording")
            if manual_paused:
                _wait()
                continue

            key = backend.frontmost()
            now = time.monotonic()

            # Auto-pause: screen frozen past the threshold -> empty chair, stop heartbeating.
            if auto_pause_on and not auto_paused and tracker.stale(now):
                auto_paused = True
                print(f"[real_capture] screen unchanged for {stale_seconds/60:.0f} min — "
                      "auto-paused (resumes on activity).")
                write_health(permission, detector, running=True, state="auto_paused")
            if auto_paused:
                # Probe for life: a window switch resumes instantly; otherwise compare a
                # cheap frame signature at heartbeat cadence.
                revived = key != state.committed
                if not revived and now - last_probe_at >= probe_interval:
                    last_probe_at = now
                    revived = tracker.observe_frame(frame_signature(backend.grab_primary()), now)
                if revived:
                    auto_paused = False
                    state = DebounceState()
                    last_emit_at = now
                    tracker.last_change_at = now
                    print("[real_capture] activity detected — capture resumed.")
                    write_health(permission, detector, running=True)
                else:
                    _wait()
                    continue

            state, emit = advance(state, key, now, debounce_seconds)
            kind = "switch"
            if not emit and heartbeat_due(state, last_emit_at, now, heartbeat_seconds):
                emit, kind, key = True, "heartbeat", state.committed
            if emit:
                app_name, window_title = key
                when = datetime.now()
                last_emit_at = now
                if dry_run:
                    print(f"[{kind}] {when:%H:%M:%S}  {app_name} — {window_title!r}")
                else:
                    img = backend.grab_primary()
                    event = make_event(app_name, window_title, when, kind=kind)
                    frame_hash = write_event(event, img, pending)
                    written += 1
                    print(f"[event]  {event.event_id}  ({kind})  {app_name} — {window_title!r}")
                    detector.observe(kind, frame_hash)
                    # Feed the staleness clock: a switch is activity by definition; a
                    # heartbeat only counts if its content actually changed.
                    if kind == "switch":
                        tracker.observe_switch(now)
                    tracker.observe_frame(frame_signature(img), now)
                    write_health(permission, detector, running=True)
                    if detector.blind and not was_blind:
                        print("[real_capture] WARNING: the last "
                              f"{detector.identical_switches} window switches produced "
                              "byte-identical screenshots — capture appears BLIND (frozen "
                              "frame / revoked Screen Recording permission). Re-grant it in "
                              "System Settings > Privacy & Security > Screen Recording.")
                    was_blind = detector.blind
                if once or (max_events is not None and written >= max_events):
                    break
            _wait()
    except KeyboardInterrupt:
        print(f"\n[real_capture] stopped. Wrote {written} event(s) to {pending}.")
    finally:
        if not dry_run:
            write_health(permission, detector, running=False)
        backend.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real event-driven screen capture for Daylook (macOS). "
                    "This captures your actual screen — see the consent note in the module docstring."
    )
    parser.add_argument("--once", action="store_true",
                        help="emit a single event and exit (smoke test)")
    parser.add_argument("--minutes", type=float, default=None,
                        help="optional: auto-stop after N minutes. Omit to run until you press "
                             "Ctrl-C (you decide when to stop).")
    parser.add_argument("--dry-run", action="store_true",
                        help="print detected switches but write nothing (no mss screenshot)")
    parser.add_argument("--poll", type=float, default=POLL_SECONDS, help="poll interval seconds")
    parser.add_argument("--debounce", type=float, default=DEBOUNCE_SECONDS,
                        help="seconds a window must persist to count as a switch")
    parser.add_argument("--heartbeat", type=float, default=HEARTBEAT_SECONDS,
                        help="seconds between stable-work heartbeat captures (0 to disable)")
    parser.add_argument("--out", type=str, default=None,
                        help="output dir (default: ~/.daylook/pending)")
    args = parser.parse_args()

    run(
        pending=Path(args.out) if args.out else None,
        poll_seconds=args.poll,
        debounce_seconds=args.debounce,
        heartbeat_seconds=args.heartbeat,
        once=args.once,
        dry_run=args.dry_run,
        duration_seconds=(args.minutes * 60) if args.minutes else None,
    )


if __name__ == "__main__":
    main()
