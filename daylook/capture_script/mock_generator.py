"""Mock capture generator (build step 1).

Stands in for the real event-driven capture layer during development. It emits the exact
same on-disk shape (``<event_id>.json`` + ``<event_id>.jpg`` in ``pending/``) that the real
capture script will, so the whole agent pipeline can be built and tested without granting
screen-recording permissions or waiting on real activity.

The scenario below is a realistic developer morning, deliberately arranged into *runs* of
related activity (several consecutive Cursor events on auth files, a burst of research tabs,
a stretch of PR review) so the Analyst Agent has genuine semantic blocks to discover.

Screenshots are synthetic but *readable*: each renders the app chrome, window title, and a
little faux content, so the vision model has real signal rather than a blank rectangle.

Run it:
    python -m daylook.capture_script.mock_generator            # write the morning to pending/
    python -m daylook.capture_script.mock_generator --clean    # wipe pending/ first
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from daylook import config
from daylook.events import CaptureEvent

# --- Scenario -------------------------------------------------------------------------
# Each segment becomes one capture event, fired when the user switches *to* that window.
# ``seconds`` is how long they stayed before the next switch (drives the next timestamp).
# All dwell times are >= a few seconds — the real capture layer debounces sub-3s flicker,
# so the mock never emits it either.

Segment = tuple[str, str, str, int]  # (app_name, window_title, faux_detail, seconds)

SCENARIO: list[Segment] = [
    ("Slack", "general — Acme Corp", "morning standup thread: who's on the release?", 240),
    ("Slack", "#eng-oncall — Acme Corp", "alert: elevated 401s on auth service overnight", 180),
    ("Google Chrome", "Inbox (3) - Gmail", "reply: re: sprint planning, JWT rotation ticket", 210),
    ("Google Chrome", "ACME-1423: Auth tokens expiring early · Jira", "bug: refresh tokens rejected after rotation", 160),
    ("Cursor", "auth.py — acme-api", "def verify_token(tok): decode + check exp", 600),
    ("Cursor", "jwt_utils.py — acme-api", "rotate_signing_key(): old kid still cached", 540),
    ("Terminal", "pytest tests/test_auth.py", "FAILED test_refresh_after_rotation", 90),
    ("Cursor", "jwt_utils.py — acme-api", "fix: keep previous key in verification set", 480),
    ("Google Chrome", "Key rotation for JWT — Stack Overflow", "answer: maintain a JWKS with overlap window", 200),
    ("Google Chrome", "JSON Web Key Set - MDN", "reading: kid header selects verification key", 180),
    ("Cursor", "jwt_utils.py — acme-api", "add jwks cache with 2-key overlap", 520),
    ("Terminal", "pytest tests/test_auth.py", "PASSED 14 passed in 3.2s", 70),
    ("Terminal", "git commit -m 'fix jwt rotation overlap'", "committed 3 files", 60),
    ("Google Chrome", "Pull Request #212 · acme/acme-api · GitHub", "open PR: fix JWT rotation overlap window", 150),
    ("Slack", "#eng-oncall — Acme Corp", "posted: PR up, should stop the 401 alerts", 120),
    ("Spotify", "Deep Focus — Playlist", "now playing: instrumental focus mix", 30),
    ("Google Chrome", "Pull Request #205: Rate limiter · GitHub", "reviewing teammate's rate-limit change", 300),
    ("Google Chrome", "Files changed · PR #205 · GitHub", "comment: nit — extract magic number", 260),
    ("Cursor", "middleware/ratelimit.py — acme-api", "checking the change out locally", 220),
    ("Google Chrome", "Pull Request #205: Rate limiter · GitHub", "approved with one suggestion", 90),
    ("Notion", "Sprint 24 — Engineering Notes", "jot: JWT rotation done, needs staging soak", 180),
    ("Google Chrome", "Vertex AI Gemini docs — Google Cloud", "reading: batching image inputs, cost notes", 240),
    ("Cursor", "analyst.py — daylook", "sketch: group events into semantic blocks", 480),
    ("Cursor", "mock_generator.py — daylook", "draft realistic morning fixture", 420),
    ("Terminal", "python -m daylook.capture_script.mock_generator", "generating mock events", 60),
    ("Slack", "general — Acme Corp", "lunch? thread", 120),
    ("Google Chrome", "The Verge — Tech News", "quick skim over lunch", 300),
    ("Spotify", "Lo-Fi Beats — Playlist", "switching the music", 25),
    ("Cursor", "journalist.py — daylook", "outline the daily writeup format", 360),
    ("Google Chrome", "Calendar - Week of Jul 6 · Google", "check afternoon meetings", 120),
]

# --- Rendering ------------------------------------------------------------------------
# Per-app accent colors so the vision model (and a human) can tell windows apart at a glance.

APP_THEME = {
    "Slack":         ((74, 21, 75),   "#slack"),
    "Google Chrome": ((32, 33, 36),   "chrome"),
    "Cursor":        ((30, 30, 30),   "cursor"),
    "Terminal":      ((12, 12, 12),   "zsh"),
    "Spotify":       ((25, 20, 20),   "spotify"),
    "Notion":        ((25, 25, 25),   "notion"),
}
DEFAULT_THEME = ((40, 40, 48), "app")

CANVAS = (1024, 640)  # already within IMAGE_MAX_EDGE, so no downscale needed for the mock


def _font(size: int) -> ImageFont.ImageFont:
    """A truetype font if we can find one, else Pillow's bitmap default."""
    for path in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_screenshot(app_name: str, window_title: str, detail: str) -> Image.Image:
    """A synthetic but legible screenshot: title bar + window title + a line of faux content."""
    accent, tag = APP_THEME.get(app_name, DEFAULT_THEME)
    img = Image.new("RGB", CANVAS, (245, 245, 247))
    draw = ImageDraw.Draw(img)

    # Title bar
    bar_h = 64
    draw.rectangle([0, 0, CANVAS[0], bar_h], fill=accent)
    # traffic lights
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        draw.ellipse([20 + i * 24, 24, 36 + i * 24, 40], fill=c)
    draw.text((110, 20), f"{app_name}", font=_font(26), fill=(235, 235, 235))
    draw.text((CANVAS[0] - 140, 24), tag, font=_font(20), fill=(180, 180, 190))

    # Window title band
    draw.rectangle([0, bar_h, CANVAS[0], bar_h + 56], fill=(225, 225, 230))
    draw.text((32, bar_h + 14), window_title, font=_font(28), fill=(30, 30, 35))

    # Faux content area
    draw.text((32, bar_h + 110), detail, font=_font(24), fill=(60, 60, 70))
    # a few filler lines to look like content
    body = _font(20)
    for i in range(6):
        w = 300 + (i * 97) % 520
        y = bar_h + 170 + i * 40
        draw.rectangle([32, y, 32 + w, y + 18], fill=(214, 214, 220))

    return img


# --- Generation -----------------------------------------------------------------------

def generate(
    pending: Path | None = None,
    start: datetime | None = None,
    clean: bool = False,
    scenario: list[Segment] | None = None,
    heartbeat_seconds: int | None = None,
) -> list[CaptureEvent]:
    """Write the scenario to ``pending`` as event pairs. Returns the events written.

    ``heartbeat_seconds`` (off by default, so existing fixtures/tests are untouched) also
    emits ``kind="heartbeat"`` events at that cadence within each segment — mimicking the
    real capture layer's stable-work heartbeats so the dense-stream pipeline path (switch-
    only batching caps, keyframe selection) can be exercised on fake data.
    """
    config.ensure_dirs()
    pending = Path(pending) if pending is not None else config.pending_dir()
    pending.mkdir(parents=True, exist_ok=True)
    if clean:
        for p in pending.iterdir():
            p.unlink() if p.is_file() else shutil.rmtree(p)

    scenario = scenario if scenario is not None else SCENARIO
    if start is None:
        # default: today at 09:02 local
        today = datetime.now().replace(hour=9, minute=2, second=0, microsecond=0)
        start = today

    events: list[CaptureEvent] = []
    t = start

    def emit(event_id: str, app: str, title: str, detail: str, when: datetime, kind: str) -> None:
        event = CaptureEvent(
            event_id=event_id,
            timestamp=when.isoformat(timespec="seconds"),
            app_name=app,
            window_title=title,
            kind=kind,
        )
        event.write_sidecar(pending)
        img = render_screenshot(app, title, detail)
        img.save(event.image_path(pending), "JPEG", quality=config.IMAGE_JPEG_QUALITY)
        events.append(event)

    # Ids are timestamp-derived, same scheme as real capture — so seeding several days (or
    # the same day twice at different hours) into one pending/ can never collide the way the
    # old sequential evt_0001.. ids did.
    for app, title, detail, seconds in scenario:
        emit(f"evt_{t:%Y%m%dT%H%M%S}", app, title, detail, t, "switch")
        if heartbeat_seconds:
            for offset in range(heartbeat_seconds, seconds, heartbeat_seconds):
                hb_t = t + timedelta(seconds=offset)
                emit(f"evt_{hb_t:%Y%m%dT%H%M%S}", app, title, detail, hb_t, "heartbeat")
        t = t + timedelta(seconds=seconds)

    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate mock Daylook capture events.")
    parser.add_argument("--clean", action="store_true", help="wipe pending/ before writing")
    parser.add_argument("--start", type=str, default=None,
                        help="ISO start time, e.g. 2026-07-05T09:00:00 (default: today 09:02)")
    parser.add_argument("--out", type=str, default=None,
                        help="output dir (default: ~/.daylook/pending)")
    parser.add_argument("--heartbeats", type=int, default=None, metavar="SECONDS",
                        help="also emit stable-work heartbeat events at this cadence "
                             f"(e.g. {config.HEARTBEAT_SECONDS}, matching real capture)")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start) if args.start else None
    out = Path(args.out) if args.out else None
    events = generate(pending=out, start=start, clean=args.clean,
                      heartbeat_seconds=args.heartbeats)

    dest = out or config.pending_dir()
    print(f"Wrote {len(events)} events to {dest}")
    print(f"  span: {events[0].timestamp}  →  {events[-1].timestamp}")
    apps = sorted({e.app_name for e in events})
    print(f"  apps: {', '.join(apps)}")


if __name__ == "__main__":
    main()
