"""Packaged-app entry point — what the double-clicked ``Daylook.app`` actually runs.

Responsibilities, in order:

  1. Translate saved user settings into the environment the google-genai SDK reads
     (``settings.apply_to_env``) *before* anything imports the agent layer, so the first model
     call sees the right token / base-URL. Also force the plain filesystem read path — a frozen
     app can't spawn the npx MCP server.
  2. Pick a local port (prefer 8642; fall back to a free one if it's taken).
  3. Start the FastAPI server bound to 127.0.0.1 (never exposed off the machine).
  4. Open the user's browser at the app once ``/health`` answers.

``--smoke`` boots the server, checks ``/health``, and exits 0/1 without opening a browser — the
build script uses it to prove the frozen bundle actually runs.

This module owns the app's environment; no ``.env`` is shipped. Run directly during dev with
``python -m daylook.launcher`` (it will use whatever settings.json / env you already have).
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from daylook import settings

DEFAULT_PORT = 8642
HOST = "127.0.0.1"


def _pick_port(preferred: int) -> int:
    """Return ``preferred`` if we can bind it, else an OS-assigned free port."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("could not find a free port to bind")


def _health_ok(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _open_when_ready(port: int, tries: int = 100) -> None:
    """Poll /health, then open the browser once. Runs in a daemon thread."""
    for _ in range(tries):
        if _health_ok(port):
            webbrowser.open(f"http://{HOST}:{port}/")
            return
        time.sleep(0.1)


def _run_server(port: int, *, blocking: bool):
    """Build a uvicorn server for the FastAPI app. Blocking (foreground) or a started thread."""
    import uvicorn

    cfg = uvicorn.Config(
        "daylook.api.main:app", host=HOST, port=port, log_level="warning"
    )
    server = uvicorn.Server(cfg)
    if blocking:
        server.run()
        return None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def _run_gui(port: int) -> int:
    """Frozen-app mode: a menubar background app (Phase 1).

    Runs the local server on a daemon thread and an AppKit event loop on the main thread. That
    loop is what makes Daylook an ambient app rather than a one-shot server:

      * a **menubar `NSStatusItem`** (status glyph + Pause/Resume, Open journal, Settings, Quit)
        is the always-there control surface — no Dock icon (LSUIElement / accessory policy),
        no browser tab popping up at login;
      * **reopen** (double-clicking the app while it's already running, or ``open -a``) opens
        the journal in the browser instead of doing nothing;
      * **system sleep/wake** notifications pause capture while the Mac sleeps and resume on
        wake (zero events, zero tokens during sleep — Phase 2);
      * **auto-start**: if the user enabled it, capture begins as soon as the server is healthy.

    Everything talks to the *same* in-process ``RootAgent`` the server uses (``get_root()`` is a
    module singleton), so the menubar and the web UI never diverge. Best-effort throughout: the
    caller wraps this in try/except and falls back to a plain blocking server if AppKit is
    unavailable, so a menubar failure can never leave the user with no app.
    """
    import webbrowser as wb

    from AppKit import (  # type: ignore
        NSApplication, NSApplicationActivationPolicyAccessory, NSMenu, NSMenuItem,
        NSStatusBar, NSVariableStatusItemLength, NSWorkspace,
        NSWorkspaceWillSleepNotification, NSWorkspaceDidWakeNotification,
    )
    from Foundation import NSObject, NSTimer  # type: ignore

    _run_server(port, blocking=False)
    url = f"http://{HOST}:{port}/"

    from daylook.api.main import get_root

    class _MenuBar(NSObject):  # noqa: N801 - ObjC naming
        # --- app delegate: reopen opens the journal ---
        def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):  # noqa: N802
            wb.open(url)
            return False

        # --- menu actions ---
        def openJournal_(self, sender):  # noqa: N802
            wb.open(url)

        def openSettings_(self, sender):  # noqa: N802
            wb.open(url)  # the auto-start toggle lives in the sidebar footer

        def togglePause_(self, sender):  # noqa: N802
            try:
                root = get_root()
                st = root.capture_status()
                if not st.get("running"):
                    root.start_capture_process()
                elif st.get("paused"):
                    root.resume_capture_process()
                else:
                    root.pause_capture_process()
            except Exception:
                pass
            self.refresh_(None)

        def quitApp_(self, sender):  # noqa: N802
            NSApplication.sharedApplication().terminate_(sender)

        # --- system sleep/wake -> capture pause/resume (Phase 2) ---
        def onSleep_(self, note):  # noqa: N802
            try:
                get_root().system_sleep()
            except Exception:
                pass

        def onWake_(self, note):  # noqa: N802
            try:
                get_root().system_wake()
            except Exception:
                pass

        # --- periodic status refresh (glyph + pause label mirror /capture/status) ---
        def refresh_(self, timer):  # noqa: N802
            try:
                st = get_root().capture_status()
            except Exception:
                st = {}
            running, paused = st.get("running"), st.get("paused")
            asleep = st.get("system_sleep") or st.get("screen_off") or st.get("auto_paused")
            if not running:
                glyph, pause_label = "○", "Start tracking"
            elif paused:
                glyph, pause_label = "❚❚", "Resume tracking"
            elif asleep:
                glyph, pause_label = "◐", "Pause tracking"
            else:
                glyph, pause_label = "●", "Pause tracking"
            try:
                self.statusItem.button().setTitle_(f"{glyph} Daylook")
                self.pauseItem.setTitle_(pause_label)
            except Exception:
                pass

    app = NSApplication.sharedApplication()
    try:
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # menubar-only, no Dock
    except Exception:
        pass
    delegate = _MenuBar.alloc().init()  # kept alive by this frame for the app's lifetime
    app.setDelegate_(delegate)

    # Build the menubar item + menu.
    status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    status_item.button().setTitle_("● Daylook")

    def _mi(title, action, key=""):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        it.setTarget_(delegate)
        return it

    menu = NSMenu.alloc().init()
    pause_item = _mi("Pause tracking", "togglePause:")
    menu.addItem_(pause_item)
    menu.addItem_(_mi("Open journal", "openJournal:"))
    menu.addItem_(_mi("Settings", "openSettings:"))
    menu.addItem_(NSMenuItem.separatorItem())
    menu.addItem_(_mi("Quit Daylook", "quitApp:", "q"))
    status_item.setMenu_(menu)
    delegate.statusItem = status_item  # pyobjc lets us stash Python attrs on the instance
    delegate.pauseItem = pause_item

    # Sleep/wake observers (workspace notification center, not the default one).
    wsnc = NSWorkspace.sharedWorkspace().notificationCenter()
    wsnc.addObserver_selector_name_object_(delegate, "onSleep:", NSWorkspaceWillSleepNotification, None)
    wsnc.addObserver_selector_name_object_(delegate, "onWake:", NSWorkspaceDidWakeNotification, None)

    # Auto-start capture once the server answers, if the user opted in. On a bg thread so the
    # main thread reaches app.run() (which drives the menubar) without waiting.
    def _autostart_when_ready():
        for _ in range(100):
            if _health_ok(port):
                break
            time.sleep(0.1)
        try:
            if settings.load().get("auto_start_capture"):
                get_root().start_capture_process()
        except Exception:
            pass
        try:
            delegate.performSelectorOnMainThread_withObject_waitUntilDone_("refresh:", None, False)
        except Exception:
            pass

    threading.Thread(target=_autostart_when_ready, daemon=True).start()

    # Live status glyph.
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        3.0, delegate, "refresh:", None, True)

    app.run()
    return 0


def _smoke(port: int) -> int:
    """Boot the server, confirm /health, tear down. Returns a process exit code."""
    server, thread = _run_server(port, blocking=False)
    try:
        for _ in range(100):
            if getattr(server, "started", False) and _health_ok(port):
                print(f"smoke: OK — server healthy on {HOST}:{port}")
                return 0
            time.sleep(0.1)
        print("smoke: FAILED — server did not become healthy", file=sys.stderr)
        return 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="daylook", description="Run the Daylook app.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="preferred local port")
    parser.add_argument("--smoke", action="store_true", help="boot, check health, exit (no browser)")
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    args = parser.parse_args(argv)

    # A frozen app can't spawn the npx MCP server — force the plain read path always, even
    # before any token is configured (so first-run /health doesn't try to launch npx).
    os.environ["DAYLOOK_USE_MCP"] = "0"
    # Translate saved settings into env before the agent layer is imported by the server.
    settings.apply_to_env()

    # Already running (a stray second launch, e.g. `open -n` or the raw binary)? Don't stand
    # up a duplicate on a random port — just bring up the existing UI and get out of the way.
    if not args.smoke and _health_ok(args.port):
        if not args.no_browser:
            webbrowser.open(f"http://{HOST}:{args.port}/")
        return 0

    port = _pick_port(args.port)

    if args.smoke:
        return _smoke(port)

    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(port,), daemon=True).start()

    # Packaged app: run the AppKit loop so re-clicking the icon reopens the browser.
    # Dev (`python -m daylook.launcher`) keeps the plain blocking server — Ctrl-C works there,
    # and NSApplication.run in a terminal doesn't.
    if getattr(sys, "frozen", False):
        try:
            return _run_gui(port)
        except Exception:
            pass  # AppKit unavailable/broken — a working server still beats no app at all

    print(f"Daylook running at http://{HOST}:{port}/  (Ctrl-C to stop)")
    _run_server(port, blocking=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
