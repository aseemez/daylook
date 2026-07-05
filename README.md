# Contextline

A local work-journal that turns raw screen activity into a semantic daily timeline.

Instead of "Cursor: 3h, Chrome: 1h", the output looks like:

- 09:15–10:00 — Debugging auth flow in Cursor
- 10:00–10:20 — Research on JWT rotation (Chrome, MDN + Stack Overflow)
- 10:20–11:30 — PR review in GitHub

Capture is **event-driven** (a new event fires when the active window/app changes, not on a
fixed clock), runs entirely locally, persists to a local store, and exposes a small HTTP API.
You talk to it through a chat interface — asking about the current day, past days, or refining
how activity is bucketed.

## Architecture

Two strictly separated layers:

- **Capture layer (dumb)** — a background script that emits a capture event (screenshot + JSON
  sidecar) on every active-window change. No reasoning. A mock generator stands in for it during
  development.
- **Agent layer (Google ADK)** — a hierarchy orchestrated by a Root Agent:
  Capture → Analyst (vision) → Journalist → Memory (SQLite), with the Root routing user queries
  and holding per-session refinements.

See [`CLAUDE.md`](./CLAUDE.md) for the full specification (architecture, storage layout, build
order, eval criteria, auth/cost discipline).

## Status

🚧 Early build — following the build order in `CLAUDE.md`. See commit history for progress.

## Stack

Python 3.11+ · Google ADK · Gemini via Vertex AI (ADC) · SQLite · FastAPI · MCP filesystem
server · pytest

## Setup

```bash
# 1. Auth (one-time) — model calls go through Vertex AI with Application Default Credentials
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID

# 2. Configure environment
cp .env.example .env   # then fill in GOOGLE_CLOUD_PROJECT

# 3. Install (once pyproject is populated)
pip install -e .
```

Local-only, dev-only. No API keys in code or environment.
