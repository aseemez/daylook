# Contextline

## Product

A local work-journal that turns raw screen activity into a semantic daily timeline.

Instead of "Cursor: 3h, Chrome: 1h", the output looks like:
- 09:15–10:00 — Debugging auth flow in Cursor
- 10:00–10:20 — Research on JWT rotation (Chrome, MDN + Stack Overflow)
- 10:20–11:30 — PR review in GitHub

Capture is **event-driven**: a new capture event fires when the active window/app changes, not on a fixed clock. This maps to real semantic boundaries (context switches) rather than arbitrary time slices, and keeps data volume and API cost low.

The user talks to the system through a chat interface — asking about the current day, past days, or refining how activity is bucketed. It runs locally, persists to a local store, and exposes a small HTTP API.

## Architecture

Two layers, kept strictly separate.

### Capture layer (dumb)

A background script. No reasoning lives here.

- Polls the frontmost app at short intervals (~1s) using `pyobjc` / `NSWorkspace` on macOS.
- On active-window/app change, produces a capture event: a screenshot (`mss`, downscaled JPEG) plus a JSON sidecar with `timestamp`, `app_name`, `window_title`, and an event id.
- Debounces rapid flicker (ignore switches under ~3s apart).
- That's it. This layer is an event source; agents do the rest.

For the initial build, a **mock capture generator** replaces this — it emits realistic fake events into the same folder shape, so the agent pipeline can be built and tested without waiting on real capture.

### Agent layer (Google ADK)

Hierarchical, orchestrated by a Root Agent.

| Agent | Responsibility |
|---|---|
| **Capture Agent** | Reads pending events via a filesystem MCP tool, batches into logical sessions or time windows, hands off to Analyst. Moves ingested events out of the pending folder. No reasoning. |
| **Analyst Agent** (Gemini, vision-capable) | Groups consecutive same-context events into semantic blocks, labels each block, computes time distribution. This is the reasoning core. Only agent that receives images. |
| **Journalist Agent** | Converts labeled blocks into a readable timeline / daily writeup. Text-only. |
| **Memory Agent** | Persists each day's journal and blocks to SQLite. Answers historical queries by reading from SQLite. Long-term memory. |
| **Root Agent** | Routes user queries, drives the pipeline, holds session state for mid-conversation refinements. |

Data flow:

```
pending/  → Capture Agent (read + batch + move to processed/)
        → Analyst Agent (label & bucket)
        → Journalist Agent (writeup)
        → Memory Agent (persist to SQLite)
        → Root Agent (serves user queries, live + historical)
```

### Where the core concepts live

- **Tools & MCP** — Capture Agent's only file-read path goes through an MCP filesystem server (`@modelcontextprotocol/server-filesystem` is the standard reference). Nothing else in the pipeline hardcodes file reads. If wiring MCP into ADK turns out to be a rabbit hole, fall back to a plain tool wrapper around filesystem reads and note MCP as the intended interface — same honest pattern used for A2A below. Don't burn hours on it.
- **Session memory** — held by the Root Agent using ADK's session service. Scoped to one chat. Stores things like active refinements ("exclude Slack from focus time") that get injected into downstream agent prompts for the rest of that session.
- **Long-term memory** — SQLite via the Memory Agent, survives across sessions, queried as a separate agent path. Distinct from session state on purpose.
- **Observability** — every agent step logs a structured record (agent, input summary, output summary, latency) to a single log file the eval script can read back.
- **Evaluation** — a standalone `eval.py` runs the pipeline against a fixed fixture and checks concrete criteria (see below). Must use the response cache so reruns are free.
- **Deployment** — a FastAPI wrapper with `/chat` and `/health`. A2A is **not implemented**; the Memory Agent is designed to be splittable into a separate service (clean interface, no shared in-process state with Root) so extracting it later is a rewrite of one file, not the pipeline.

## Storage layout

```
~/.contextline/
├── pending/          # capture events awaiting processing
│   ├── evt_<id>.jpg
│   └── evt_<id>.json
├── processed/        # moved here after Capture Agent ingests
├── cache/            # on-disk response cache (see Auth & cost)
└── journal.sqlite    # long-term memory
```

SQLite schema — minimum shape, adjust as needed:

- `days(date PRIMARY KEY, writeup TEXT, created_at TIMESTAMP)`
- `blocks(id INTEGER PRIMARY KEY, date TEXT, start TIMESTAMP, end TIMESTAMP, label TEXT, app_name TEXT, source_event_ids JSON)`

`source_event_ids` stores the list of capture event ids that were merged into that block. The anti-hallucination check in `eval.py` uses this to verify every block traces back to real captured data.

## Session refinements

The Root Agent maintains a small state object per session (via ADK session service) that captures user-issued refinements — things like an exclusion list of apps, a preferred bucket granularity, or a focus-time definition. Whenever the Root calls downstream agents in that session, these refinements are threaded into their prompts. The refinements don't persist across sessions and don't touch SQLite — they're conversational.

## Build order

Ship something runnable at every step. Do not build all agents in isolation before wiring.

1. **Mock capture generator** — writes fake events into `pending/`. Unblocks everything downstream.
2. **Capture Agent** — reads and batches via a filesystem MCP tool (or plain tool wrapper if MCP is blocking), moves ingested events to `processed/`. Verify against mock events.
3. **Analyst Agent** — labels blocks. Test in isolation with a hand-written batch fixture before wiring to Capture.
4. **Journalist Agent** — writeup. Same pattern: test with a hand-written blocks fixture first.
5. **Memory Agent + SQLite** — persist and query. Verify a full write/read cycle.
6. **Root Agent** — orchestrate the four, add session refinements.
7. **Structured logging** — wrap every agent call.
8. **`eval.py`** — see criteria below.
9. **FastAPI wrapper** — `/chat`, `/health`, local only.
10. **Real event-driven capture script** — only if 1–9 are working.

## Testing

Each agent gets a small pytest module with fixture-based input. Fixtures live in `tests/fixtures/`:

- `capture_events.json` — a batch of ~30 mock events spanning a realistic morning.
- `labeled_blocks.json` — expected shape of Analyst output (for Journalist tests).
- `sample_journal.json` — expected shape for Memory persistence tests.

Integration test: run the full pipeline against `capture_events.json`, assert SQLite has a journal row for the target date, assert Root Agent answers a query about that day correctly.

### `eval.py` criteria

Runs the pipeline against a fixed fixture and scores the output:

1. Output contains time-bucketed blocks, not just prose.
2. **No hallucinated app names** — every `app_name` in output blocks appears in the source capture events (cross-check via `source_event_ids`).
3. Memory persistence confirmed — after run, query SQLite for the date; row exists and matches the writeup.
4. Analytical content present — at least one time distribution or ranking.
5. Session refinement respected — inject an exclusion mid-session, verify subsequent responses honor it.

Each criterion is a boolean; the script prints per-criterion pass/fail and an overall score.

Reruns must hit the on-disk cache (see below). If a rerun of the same fixture makes real API calls, that's a bug.

## Tech stack

- Python 3.11+
- Google ADK (agents + orchestration, session service)
- Gemini via **Vertex AI** (not the AI Studio API) — see Auth & cost
- MCP filesystem server for the Capture Agent's read path (with plain-tool fallback)
- SQLite (long-term memory)
- FastAPI (local HTTP wrapper)
- `mss` (screenshots), `pyobjc` (frontmost app) — real capture script only
- pytest for tests

## Auth & cost

Model calls go through **Vertex AI, authenticated with Application Default Credentials (ADC)**, billed against a Google Cloud project with free-trial credits. No API keys in code or env.

One-time setup on the dev machine:

```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Required env vars (put in `.env`, loaded on startup):

```
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
GOOGLE_CLOUD_LOCATION=us-central1
```

With these, ADK routes through Vertex automatically. Agent code stays clean:

```python
from google.adk.agents import LlmAgent
analyst = LlmAgent(model="gemini-2.5-flash", name="analyst", ...)
```

**Cost discipline**:

- Default model everywhere is `gemini-2.5-flash`. Do not use `gemini-2.5-pro` unless a specific step demonstrably fails on flash.
- Only the Analyst Agent receives images. Everything else is text-only.
- Screenshots are downscaled to max 1024px on the long edge, JPEG q≈70, before being sent to the model.
- Batch Analyst calls: one call per session/window, not one call per event.
- **On-disk response cache** at `~/.contextline/cache/`, keyed on a hash of the input batch. Every Analyst call checks the cache first. This makes rerunning the pipeline or eval against the same fixture cost nothing after the first run — critical during iteration.
- Fallback if credits get tight: swap Analyst's model for `gemini-2.5-flash-lite`, don't change the architecture.

## FastAPI wrapper

Minimal, local-only, no auth (dev-only, note this in the writeup).

- `POST /chat` — JSON body `{"message": str, "session_id": str | null}`. Response `{"reply": str, "session_id": str}`. Sync response (no streaming for v1 — streaming can be added later without changing the agent layer).
- `GET /health` — returns 200 with a small JSON status payload.

The session_id maps directly to an ADK session; if null, a new session is created and returned.

## Repo layout

```
contextline/
├── agents/
│   ├── capture.py
│   ├── analyst.py
│   ├── journalist.py
│   ├── memory.py
│   └── root.py
├── capture_script/
│   ├── mock_generator.py
│   └── real_capture.py       # last, optional
├── storage/
│   └── sqlite.py
├── api/
│   └── main.py               # FastAPI app
├── tests/
│   ├── fixtures/
│   └── test_*.py
├── eval.py
├── .env.example
├── pyproject.toml
├── CLAUDE.md
└── README.md
```

## Working notes

- Capture layer stays dumb. If reasoning starts creeping into it, move it into the Capture Agent.
- Every agent should be independently runnable with a fixture — no agent should require the full pipeline to test.
- Don't fake persistence or caching. If either isn't wired, the eval should fail, and that's the point.
- Where the CLAUDE.md doesn't specify an implementation detail, that's usually intentional — decide based on what fits and note the decision in a code comment. If a decision has repo-wide implications (schema change, storage relocation, an agent boundary shift), reflect it back in this file so future sessions stay consistent.

## Current status (2026-07-05)

**Build steps 1–9 of 10 are complete, tested, and pushed** to https://github.com/aseemez/contextline
(`main` clean). Pipeline runs end to end on real Vertex AI (Gemini 2.5 Flash), scores **5/5 on
`eval.py`**, and serves over HTTP. Tests: **43 passed, 6 skipped**. Only **step 10 (real macOS
capture script) remains** — it's optional/last by design; the project is complete without it.

👉 **Read `PROGRESS.md` for the full handoff**: what's done per step, environment facts (Python 3.14,
ADK 2.3.0, `GOOGLE_CLOUD_LOCATION=global`, ADC/no-keys, on-disk cache makes reruns free), decisions
already made (don't re-open), decisions still open (chiefly: whether/how to do step 10 — the
real-screen-capture path is **consent-gated**, the user rejected installing `mss`/`pyobjc` earlier),
and run commands. Also flagged there: a plaintext `STRIPE_SECRET_KEY` in the user's `~/.zshrc`.