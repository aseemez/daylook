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

## Status

**Complete and running.** The full pipeline works end to end against real Vertex AI
(Gemini 2.5 Flash), scores **5/5 on `eval.py`**, and serves over HTTP. Test suite: **48 passed,
6 skipped** (skips are opt-in live-LLM tests + an MCP-needs-npx case).

All ten build steps from [`CLAUDE.md`](./CLAUDE.md) are implemented, including the real macOS
capture script — see [Real screen capture](#real-screen-capture-macos) for the consent note.
See [`PROGRESS.md`](./PROGRESS.md) for the detailed per-step handoff.

## Architecture

Two strictly separated layers:

- **Capture layer (dumb)** — emits a capture event (screenshot + JSON sidecar) on every
  active-window change. No reasoning. A [mock generator](contextline/capture_script/mock_generator.py)
  stands in during development; [`real_capture.py`](contextline/capture_script/real_capture.py)
  is the live macOS version.
- **Agent layer (Google ADK)** — a hierarchy orchestrated by a Root Agent:

  ```
  pending/ → Capture Agent  (read + batch + move to processed/, via MCP filesystem tool)
           → Analyst Agent   (vision: group + label into semantic blocks)
           → Journalist Agent (readable daily writeup)
           → Memory Agent     (persist to SQLite; answer historical queries)
           → Root Agent       (routes user queries, holds per-session refinements)
  ```

Key design decisions (see `CLAUDE.md` / `PROGRESS.md` for the full list):

- **Anti-hallucination is structural, not prompt-based.** The Analyst LLM only *groups and
  labels*; every fact (times, durations, app names, `source_event_ids`) is derived in code from
  the real events, so a block can always be traced back to captured data.
- **On-disk response cache** keyed on the exact input batch — the first run costs, every rerun
  of the same fixture is free. Critical for iterating on the pipeline and eval without burning
  API credits.
- **Session refinements** ("exclude Slack from focus time") live in ADK session state, are
  threaded into downstream prompts for that chat only, and never touch SQLite.

## Stack

Python 3.11+ · Google ADK · Gemini via Vertex AI (ADC, no API keys) · SQLite · FastAPI ·
MCP filesystem server (with a plain-tool fallback) · pytest

## Setup

```bash
# 1. Auth (one-time) — model calls go through Vertex AI with Application Default Credentials
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID

# 2. Configure environment
cp .env.example .env    # fill in GOOGLE_CLOUD_PROJECT; keep GOOGLE_CLOUD_LOCATION=global

# 3. Install
pip install -e ".[dev]"
```

`.env` carries only Vertex routing (`GOOGLE_GENAI_USE_VERTEXAI=TRUE`, `GOOGLE_CLOUD_PROJECT`,
`GOOGLE_CLOUD_LOCATION=global`). Real credentials come from ADC — **no API keys in code or env.**

## Running it

```bash
# Seed a realistic fake morning into ~/.contextline/pending
python -m contextline.capture_script.mock_generator

# Score the 5 eval criteria (free after the first run, thanks to the cache)
python eval.py

# Run the tests
pytest                    # 48 passed, 6 skipped — hermetic, no API calls
RUN_LLM_TESTS=1 pytest    # include live-model tests (first run costs)

# Serve the HTTP API, then POST /chat and GET /health
uvicorn contextline.api.main:app --reload
```

`POST /chat` takes `{"message": str, "session_id": str | null}` and returns
`{"reply": str, "session_id": str}`. `GET /health` returns a small status payload. Local-only,
no auth — dev only.

## Real screen capture (macOS)

`contextline/capture_script/real_capture.py` is the live, event-driven capture layer: it polls
the frontmost app (~1s) via `pyobjc`/`NSWorkspace`, and on a debounced window change grabs a
downscaled screenshot (`mss`) plus a JSON sidecar into `pending/` — the *same on-disk shape* as
the mock generator, so the rest of the pipeline is unchanged.

**Running it captures your real screen**, so its dependencies are an opt-in extra and are not
installed by default. Importing the module is always safe; only invoking it starts capturing.

```bash
pip install -e ".[capture]"                                   # mss + pyobjc (macOS)
python -m contextline.capture_script.real_capture --dry-run   # print switches, write nothing
python -m contextline.capture_script.real_capture --once      # emit a single event and exit
python -m contextline.capture_script.real_capture             # capture until Ctrl-C
```

Window *titles* additionally require macOS Screen Recording permission; without it capture
still works at the app level (empty title).
