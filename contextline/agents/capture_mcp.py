"""MCP filesystem read path for the Capture Agent — the *intended* interface.

CLAUDE.md: "the Capture Agent's only file-read path goes through an MCP filesystem server
(``@modelcontextprotocol/server-filesystem`` is the standard reference)." This module does
exactly that: it spawns that server over stdio and reads the pending event sidecars through
MCP tool calls (``list_directory`` + ``read_multiple_files``), rather than touching the
filesystem directly.

Only the *read* path goes through MCP (that's what the spec constrains). Moving ingested
events to ``processed/`` stays a plain filesystem operation in ``capture.py``.

If Node/npx or the ``mcp`` package isn't available, ``make_event_source`` transparently falls
back to the plain ``FilesystemEventSource`` — the honest fallback the spec allows, so the
pipeline is never blocked on MCP.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from contextline import config
from contextline.agents.capture import EventSource, FilesystemEventSource
from contextline.events import CaptureEvent

SERVER_PACKAGE = "@modelcontextprotocol/server-filesystem"


def mcp_available() -> bool:
    """True if we can plausibly run the MCP filesystem server (npx present + mcp importable)."""
    if shutil.which("npx") is None:
        return False
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


class MCPEventSource:
    """Reads pending event sidecars through an MCP filesystem server (stdio transport)."""

    def __init__(self, pending: Path | None = None):
        self.pending = Path(pending) if pending is not None else config.pending_dir()

    def list_events(self) -> list[CaptureEvent]:
        # Sync facade over the async MCP client — one server spawn per ingest, which is fine.
        return asyncio.run(self._list_events_async())

    async def _list_events_async(self) -> list[CaptureEvent]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not self.pending.exists():
            return []

        params = StdioServerParameters(
            command="npx",
            args=["-y", SERVER_PACKAGE, str(self.pending)],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                listing = await session.call_tool(
                    "list_directory", {"path": str(self.pending)}
                )
                names = _parse_listing(_text(listing))
                json_paths = [str(self.pending / n) for n in names if n.endswith(".json")]
                if not json_paths:
                    return []

                # read_multiple_files is one round-trip for the whole batch
                result = await session.call_tool(
                    "read_multiple_files", {"paths": json_paths}
                )
                events = _parse_multi_read(_text(result))

        return sorted(events, key=lambda e: (e.timestamp, e.event_id))


# --- helpers to parse the filesystem server's text responses --------------------------

def _text(result) -> str:
    if not getattr(result, "content", None):
        return ""
    return "".join(getattr(c, "text", "") for c in result.content)


def _parse_listing(text: str) -> list[str]:
    """The server prints entries like ``[FILE] evt_0001.json`` / ``[DIR] sub``."""
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # strip a leading "[FILE] " / "[DIR] " tag if present
        if line.startswith("[") and "]" in line:
            line = line.split("]", 1)[1].strip()
        names.append(line)
    return names


def _parse_multi_read(text: str) -> list[CaptureEvent]:
    """read_multiple_files returns blocks separated by ``---``, each ``path:\n<contents>``."""
    events: list[CaptureEvent] = []
    for block in text.split("\n---\n"):
        block = block.strip()
        if not block:
            continue
        # split off the "path:" header from the JSON body
        _, _, body = block.partition("\n")
        body = body.strip() or block
        start = body.find("{")
        if start == -1:
            continue
        try:
            data = json.loads(body[start:])
            events.append(CaptureEvent.from_dict(data))
        except (json.JSONDecodeError, KeyError):
            continue
    return events


def make_event_source(
    pending: Path | None = None,
    use_mcp: bool | None = None,
) -> EventSource:
    """Pick the read path. Prefers MCP (the intended interface); falls back to plain FS.

    ``use_mcp``: True/False forces it; None consults ``CONTEXTLINE_USE_MCP`` (default on).
    If MCP is requested but unavailable, we fall back rather than fail — and say so via log.
    """
    import logging
    import os

    log = logging.getLogger("contextline.capture")
    if use_mcp is None:
        use_mcp = os.environ.get("CONTEXTLINE_USE_MCP", "1").lower() not in ("0", "false", "no")

    if use_mcp and mcp_available():
        log.info("Capture read path: MCP filesystem server (%s)", SERVER_PACKAGE)
        return MCPEventSource(pending)

    if use_mcp:
        log.warning("MCP requested but unavailable (npx/mcp missing); using plain filesystem read path")
    else:
        log.info("Capture read path: plain filesystem (MCP disabled)")
    return FilesystemEventSource(pending)
