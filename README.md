# Contextline

A local work-journal that turns raw screen activity into a semantic daily timeline.

When you spend all day at a computer, the hours blur together and it is hard to remember what you
actually worked on. Contextline watches your screen activity and reconstructs your day as
readable activities, so you can look back, understand your habits, and plan a better tomorrow.

Instead of "Cursor: 3h, Chrome: 1h", the output looks like:

- 09:15–10:00 — Debugging auth flow in Cursor
- 10:00–10:20 — Research on JWT rotation (Chrome, MDN + Stack Overflow)
- 10:20–11:30 — PR review in GitHub

Capture is **event-driven**: a new event fires when the active window or app changes, not on a
fixed clock. It runs entirely on your machine, stores everything locally, and exposes a small HTTP
API. You talk to it through a chat interface, asking about the current day, past days, or how you
want activity grouped.

## Status

**Complete and running.** The full pipeline works end to end against real Vertex AI
(Gemini 2.5 Flash), scores **5/5 on `eval.py`**, and serves over HTTP. Test suite: **48 passed,
6 skipped** (the skips are opt-in live-model tests plus one MCP case that needs `npx`).

All ten build steps from [`CLAUDE.md`](./CLAUDE.md) are implemented, including the real macOS
capture script. See [Real screen capture](#real-screen-capture-macos) for the consent note, and
[`PROGRESS.md`](./PROGRESS.md) for the detailed per-step handoff.

## How it works

Contextline has two layers, kept strictly separate.

**The capture layer is deliberately simple.** It records what is on screen and nothing more. On
every active-window change it writes a capture event: a screenshot plus a small JSON file with the
app name, window title, and timestamp. A [mock generator](contextline/capture_script/mock_generator.py)
stands in during development, and [`real_capture.py`](contextline/capture_script/real_capture.py)
is the live macOS version. Both write the same files, so nothing downstream cares which one ran.

**The agent layer does all the thinking**, built on Google ADK as a hierarchy of five agents that
a Root Agent coordinates:

```
pending/ → Capture Agent   (read + batch events, move to processed/, via an MCP filesystem tool)
         → Analyst Agent    (vision: group events and label them into semantic blocks)
         → Journalist Agent (turn the blocks into a readable daily writeup)
         → Memory Agent      (save each day to SQLite, answer questions about past days)
         → Root Agent        (the chat you talk to; holds per-session preferences)
```

Three design decisions are worth calling out (the full list is in `CLAUDE.md` and `PROGRESS.md`):

- **Nothing is hallucinated, by construction.** The Analyst model only groups events and labels
  them. Every hard fact (times, durations, app names, source event IDs) is computed in code from
  the real events, so every block can be traced back to activity that was actually captured.
- **Reruns are free.** An on-disk cache stores each model call keyed on its exact input. The first
  run costs money; every rerun on the same data is served from disk. This is what makes iterating
  on the pipeline and the eval affordable.
- **Conversational preferences stay conversational.** Refinements like "don't count Slack as focus
  time" live in ADK session state and are threaded into later prompts for that chat only. They
  never touch the database.

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
`GOOGLE_CLOUD_LOCATION=global`). Real credentials come from ADC, so there are **no API keys in code
or env.**

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
`{"reply": str, "session_id": str}`. `GET /health` returns a small status payload. It is local-only
with no auth, which is fine for a personal dev tool.

## Real screen capture (macOS)

`contextline/capture_script/real_capture.py` is the live, event-driven capture layer. It polls the
frontmost app about once a second via `pyobjc`/`NSWorkspace`, and on a debounced window change it
grabs a downscaled screenshot (`mss`) plus a JSON sidecar into `pending/`. These are the same files
the mock generator writes, so the rest of the pipeline is unchanged.

**Running it captures your real screen,** so its dependencies are an opt-in extra and are not
installed by default. Importing the module is always safe; only invoking it starts capturing.

```bash
pip install -e ".[capture]"                                   # mss + pyobjc (macOS)
python -m contextline.capture_script.real_capture --dry-run   # print switches, write nothing
python -m contextline.capture_script.real_capture --once      # emit a single event and exit
python -m contextline.capture_script.real_capture             # capture until Ctrl-C
```

Window *titles* additionally require macOS Screen Recording permission. Without it, capture still
works at the app level with an empty title.
