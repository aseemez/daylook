"""Real event-driven capture layer (build step 10) — macOS.

The *dumb* capture layer: it polls the frontmost application ~once a second and, when the
active window/app changes (and the change survives a short debounce), emits one capture event
— a downscaled JPEG screenshot plus a JSON sidecar — into ``pending/``. No reasoning lives
here; that's the Capture/Analyst Agents' job. This writes the **exact same on-disk shape** as
``mock_generator.py`` (``<event_id>.json`` + ``<event_id>.jpg``), so nothing downstream changes.

Design notes
------------
- **Event-driven, not clock-driven.** We poll on a short interval only to *detect* the switch;
  an event is emitted on an active-window change, not every tick. This maps to real semantic
  boundaries (context switches) and keeps data volume + API cost low.
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
``python -m contextline.capture_script.real_capture``) will start grabbing screenshots. That's
a deliberate, explicit action; nothing captures your screen just by importing.

Run it:
    python -m contextline.capture_script.real_capture            # capture until Ctrl-C
    python -m contextline.capture_script.real_capture --once     # emit one event and exit (smoke test)
    python -m contextline.capture_script.real_capture --dry-run  # print switches, write nothing
"""

from __future__ import annotations

import argparse
import io
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from contextline import config
from contextline.events import CaptureEvent

# --- Tunables -------------------------------------------------------------------------
POLL_SECONDS = 1.0          # how often we sample the frontmost window
DEBOUNCE_SECONDS = 3.0      # a window must persist this long before it counts as a real switch

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
    """Long edge <= ``IMAGE_MAX_EDGE``. Same discipline as :mod:`contextline.imaging`."""
    long_edge = max(im.size)
    if long_edge > config.IMAGE_MAX_EDGE:
        scale = config.IMAGE_MAX_EDGE / long_edge
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    return im


def make_event(app_name: str, window_title: str, when: datetime) -> CaptureEvent:
    """Build a :class:`CaptureEvent` with a timestamp-derived, sortable, unique id.

    ``evt_<YYYYMMDDTHHMMSS>`` — events are >= DEBOUNCE_SECONDS apart, so same-second ids can't
    collide within a run, and the format sorts identically to its timestamp.
    """
    return CaptureEvent(
        event_id=f"evt_{when:%Y%m%dT%H%M%S}",
        timestamp=when.isoformat(timespec="seconds"),
        app_name=app_name,
        window_title=window_title,
    )


def write_event(event: CaptureEvent, image: Image.Image, pending: Path) -> None:
    """Persist the sidecar + downscaled JPEG — the same pair shape the mock generator writes."""
    event.write_sidecar(pending)
    _downscale(image).save(event.image_path(pending), "JPEG", quality=config.IMAGE_JPEG_QUALITY)


# --- Capture loop ---------------------------------------------------------------------

def run(
    pending: Path | None = None,
    poll_seconds: float = POLL_SECONDS,
    debounce_seconds: float = DEBOUNCE_SECONDS,
    once: bool = False,
    dry_run: bool = False,
    max_events: int | None = None,
    duration_seconds: float | None = None,
) -> int:
    """Poll the frontmost window and emit an event on each debounced context switch.

    Runs until Ctrl-C, or until any of the optional stop conditions is reached: ``once`` (one
    event), ``max_events`` (a count), or ``duration_seconds`` (a wall-clock limit — the "run it
    for 5 minutes and stop" control). Returns the number of events written. ``dry_run`` prints
    switches without writing anything (and needs no ``mss``).
    """
    config.ensure_dirs()
    pending = Path(pending) if pending is not None else config.pending_dir()
    pending.mkdir(parents=True, exist_ok=True)

    backend = _load_backend(need_screenshots=not dry_run)
    state = DebounceState()
    written = 0
    start = time.monotonic()

    stop_note = "Ctrl-C to stop" if duration_seconds is None else (
        f"auto-stops in {duration_seconds/60:.0f} min (or Ctrl-C)")
    print(f"[real_capture] watching frontmost window (poll={poll_seconds}s, "
          f"debounce={debounce_seconds}s){'  [DRY RUN]' if dry_run else ''}. {stop_note}.")
    try:
        while True:
            if duration_seconds is not None and time.monotonic() - start >= duration_seconds:
                print(f"\n[real_capture] reached {duration_seconds/60:.0f}-min limit. "
                      f"Wrote {written} event(s) to {pending}.")
                break
            key = backend.frontmost()
            state, emit = advance(state, key, time.monotonic(), debounce_seconds)
            if emit:
                app_name, window_title = key
                when = datetime.now()
                if dry_run:
                    print(f"[switch] {when:%H:%M:%S}  {app_name} — {window_title!r}")
                else:
                    event = make_event(app_name, window_title, when)
                    write_event(event, backend.grab_primary(), pending)
                    written += 1
                    print(f"[event]  {event.event_id}  {app_name} — {window_title!r}")
                if once or (max_events is not None and written >= max_events):
                    break
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        print(f"\n[real_capture] stopped. Wrote {written} event(s) to {pending}.")
    finally:
        backend.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real event-driven screen capture for Contextline (macOS). "
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
    parser.add_argument("--out", type=str, default=None,
                        help="output dir (default: ~/.contextline/pending)")
    args = parser.parse_args()

    run(
        pending=Path(args.out) if args.out else None,
        poll_seconds=args.poll,
        debounce_seconds=args.debounce,
        once=args.once,
        dry_run=args.dry_run,
        duration_seconds=(args.minutes * 60) if args.minutes else None,
    )


if __name__ == "__main__":
    main()
