"""Packaged-app entry point — what the double-clicked ``Contextline.app`` actually runs.

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
``python -m contextline.launcher`` (it will use whatever settings.json / env you already have).
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

from contextline import settings

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
        "contextline.api.main:app", host=HOST, port=port, log_level="warning"
    )
    server = uvicorn.Server(cfg)
    if blocking:
        server.run()
        return None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


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
    parser = argparse.ArgumentParser(prog="contextline", description="Run the Contextline app.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="preferred local port")
    parser.add_argument("--smoke", action="store_true", help="boot, check health, exit (no browser)")
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    args = parser.parse_args(argv)

    # A frozen app can't spawn the npx MCP server — force the plain read path always, even
    # before any token is configured (so first-run /health doesn't try to launch npx).
    os.environ["CONTEXTLINE_USE_MCP"] = "0"
    # Translate saved settings into env before the agent layer is imported by the server.
    settings.apply_to_env()

    port = _pick_port(args.port)

    if args.smoke:
        return _smoke(port)

    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(port,), daemon=True).start()
    print(f"Contextline running at http://{HOST}:{port}/  (Ctrl-C to stop)")
    _run_server(port, blocking=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
