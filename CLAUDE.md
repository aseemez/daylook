# Daylook

## Product

A local work-journal that turns raw screen activity into a semantic daily timeline.

Instead of "Cursor: 3h, Chrome: 1h", the output looks like:
- 09:15–10:00 — Debugging auth flow in Cursor
- 10:00–10:20 — Research on JWT rotation (Chrome, MDN + Stack Overflow)
- 10:20–11:30 — PR review in GitHub

Capture is **hybrid**: a `switch` event fires when the active window/app changes (the semantic boundary), and a `heartbeat` event fires every ~12s while the user stays stably in the same window (proof of presence). Switches carry the meaning; heartbeats carry the time — without them, a 2-hour deep-work session is one event and gets credited almost nothing. Cost stays low anyway because heartbeats are cheap riders: only *switches* count toward the vision-batch cap, and at most `MAX_IMAGES_PER_BATCH` keyframe screenshots go to the model per call (all switches + evenly-sampled heartbeats; the rest ride as text metadata).

The user talks to the system through a chat interface — asking about the current day, past days, or refining how activity is bucketed. It runs locally, persists to a local store, and exposes a small HTTP API.

## Architecture

Two layers, kept strictly separate.

### Capture layer (dumb)

A background script. No reasoning lives here.

- Polls the frontmost app at short intervals (~1s) using `pyobjc` / CGWindowList z-order on macOS.
- On active-window/app change, produces a `switch` capture event: a screenshot (`mss`, downscaled JPEG) plus a JSON sidecar with `timestamp`, `app_name`, `window_title`, `kind`, and an event id.
- While the committed window stays stable, emits a `heartbeat` event every `HEARTBEAT_SECONDS` (~12s) — same on-disk shape, `kind: "heartbeat"`. Heartbeats start only after the debounce confirms the user is stably on something.
- Debounces rapid flicker (ignore switches under ~3s apart); no heartbeats while a switch candidate is pending.
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
~/.daylook/
├── pending/          # capture events awaiting processing
│   ├── evt_<id>.jpg
│   └── evt_<id>.json
├── processed/        # moved here after Capture Agent ingests
├── cache/            # on-disk response cache (see Auth & cost)
└── journal.sqlite    # long-term memory
```

SQLite schema — minimum shape, adjust as needed:

- `days(date PRIMARY KEY, writeup TEXT, created_at TIMESTAMP)`
- `blocks(id INTEGER PRIMARY KEY, date TEXT, start TIMESTAMP, end TIMESTAMP, label TEXT, app_name TEXT, category TEXT, duration_seconds INT, source_event_ids JSON, manual INT DEFAULT 0)`

`source_event_ids` stores the list of capture event ids that were merged into that block. The anti-hallucination check in `eval.py` uses this to verify every block traces back to real captured data.

`manual=1` marks a block the user added or edited by hand in the calendar UI (its `source_event_ids` is `[]` — it traces to the user, not to capture). Manual blocks **survive re-processing** of the same day: `save_day` only clears `manual=0` rows. Editing goes through `MemoryAgent.add_block/update_block/delete_block` (the store's only write path stays Memory) and is exposed by `POST/PATCH/DELETE /journal/{date}/blocks[/{id}]` in the API. Column added via an in-place migration in `Store._migrate` so existing DBs upgrade without data loss. The anti-hallucination invariant still holds for *pipeline* output; manual blocks are a deliberate user-authored exception and are never part of eval fixtures.

## Data profiles (dev vs real — do not mix)

`DAYLOOK_HOME` relocates the entire storage tree (see `daylook/config.py:_home`). Three profiles, strictly separated:

- **Real** — `~/.daylook` (the default when `DAYLOOK_HOME` is unset). This is the owner's personal journal. **Agent sessions must never write here uninvited** — no mock seeding, no test pipelines, no `adk web` runs against it. Real screen capture stays consent-gated as before.
- **Dev/test** — `<repo>/.dev_home`, gitignored. All development work goes here. Use the wrappers: `scripts/dev-seed.sh` (mock events), `scripts/dev-api.sh` (uvicorn), `scripts/dev-adk-web.sh` (ADK dev UI), or `source scripts/dev-env.sh` in a shell.
- **Eval** — `<repo>/.eval_home`, owned by `eval.py` (it sets the env var itself). Don't touch.

`pytest` already isolates via `monkeypatch` in conftest. One-time cleanup of the previously-mixed real home: `scripts/archive-real-home.sh` (moves journal data into a dated `archive-*/` subfolder, keeps `cache/`, deletes nothing).

Never set `GOOGLE_GEMINI_BASE_URL` in repo `.env` or dev shells — google-genai honors it even when Vertex is configured, so it would silently hijack dev model calls. That variable belongs only to the packaged end-user app / relay tests (see `DISTRIBUTION.md`).

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
- Gemini via the **Gemini Developer API** (free-tier AI Studio key in dev; the relay's key in prod) — see Auth & cost
- MCP filesystem server for the Capture Agent's read path (with plain-tool fallback)
- SQLite (long-term memory)
- FastAPI (local HTTP wrapper)
- `mss` (screenshots), `pyobjc` (frontmost app) — real capture script only
- pytest for tests

## Auth & cost

Three routes to a Gemini model, all through the same `GOOGLE_API_KEY` seam the google-genai SDK reads. **Never mix them in one environment.**

1. **Dev/test (this repo)** — a **free-tier Gemini Developer API key** (make one at https://aistudio.google.com/apikey). `.env` carries `GOOGLE_GENAI_USE_VERTEXAI=FALSE` + `GOOGLE_API_KEY`. No GCP project, no gcloud, no billing. Free-tier daily limits are fine for dev because the response cache makes reruns free.
2. **End users (packaged app)** — an invite token sent to the **relay** (`relay/`), which holds the real provider credential server-side and forwards. Users never hold a Google key. See `DISTRIBUTION.md`.
3. **Legacy: Vertex + ADC** — how dev used to run and how the currently-deployed relay authenticates upstream (bills the GCP trial project). Still supported (`GOOGLE_GENAI_USE_VERTEXAI=TRUE` + project vars) but not the default anywhere; the relay's production mode is a `GEMINI_API_KEY` env var on a real billing account with provider-side quota caps.

Required dev env vars (put in `.env`, loaded on startup):

```
GOOGLE_GENAI_USE_VERTEXAI=FALSE
GOOGLE_API_KEY=<free-tier AI Studio key>
```

With these, ADK routes to the Gemini Developer API automatically. Agent code stays clean:

```python
from google.adk.agents import LlmAgent
analyst = LlmAgent(model="gemini-2.5-flash", name="analyst", ...)
```

**Cost discipline**:

- Default model everywhere is `gemini-2.5-flash`. Do not use `gemini-2.5-pro` unless a specific step demonstrably fails on flash.
- Only the Analyst Agent receives images. Everything else is text-only.
- Screenshots are downscaled to max 1024px on the long edge, JPEG q≈70, before being sent to the model.
- Batch Analyst calls: one call per session/window, not one call per event. Only switch events count toward the batch cap; heartbeats don't shatter batches.
- Keyframe selection: at most `MAX_IMAGES_PER_BATCH` (12) screenshots per vision call — every switch plus evenly-sampled heartbeats. A heartbeat-dense focus hour is still one call with ≤12 images, so heartbeats add time-accounting precision at roughly zero marginal model cost (~$1–3/user/month on Flash for a full tracked day).
- **On-disk response cache** at `~/.daylook/cache/`, keyed on a hash of the input batch. Every Analyst call checks the cache first. This makes rerunning the pipeline or eval against the same fixture cost nothing after the first run — critical during iteration.
- Fallback if credits get tight: swap Analyst's model for `gemini-2.5-flash-lite`, don't change the architecture.

## FastAPI wrapper

Minimal, local-only, no auth (dev-only, note this in the writeup).

- `POST /chat` — JSON body `{"message": str, "session_id": str | null}`. Response `{"reply": str, "session_id": str}`. Sync response (no streaming for v1 — streaming can be added later without changing the agent layer).
- `GET /health` — returns 200 with a small JSON status payload.

The session_id maps directly to an ADK session; if null, a new session is created and returned.

## Repo layout

```
daylook/
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

## Production plan (decided 2026-07-21 — don't re-litigate)

Target: **~10 paying users, macOS only** (Windows is a later port of the capture layer only). The comparison product is [Dayflow](https://github.com/JerryZLiu/dayflow); Daylook's differentiators are event-driven capture (cheaper, semantic) and **zero-setup AI** (users get a token, not "go make a Google API key").

- **Business shape** — the standard wrapper-SaaS pattern: relay holds the provider credential, each customer gets a `cl_...` token, **paid from day 1 — no free tier**. Three Stripe Payment Links on the landing page: **$5/week**, **$20/month**, **$16/month billed yearly ($192/yr)**. Owner emails a token on payment (manual is correct at n=10; a Stripe webhook that mints tokens is the n=100 upgrade). Unit economics: heavy usage ≈ $1–3/user/month in Gemini Flash cost against $16–20 revenue.
- **Product bar** — "Cursor-grade": the user pays, downloads, pastes a code, and it just works — no keys, no config, no terminal. Anything that breaks that (Gatekeeper scare, relay cold-start lag, silent capture failures) is a product bug, not a nice-to-have; the checklist lives in `DISTRIBUTION.md`.
- **AI billing** — relay upstream moves off trial-credit Vertex/ADC to a **`GEMINI_API_KEY` on a real billing account** (the seam is built; it's one env var on redeploy). Cost control = provider-side quota caps on the key + budget alert; relay's in-memory counters are an abuse throttle, not billing truth.
- **Dev/test** — free-tier AI Studio key (see Auth & cost). Never bill dev work to the production key.
- **Trust upgrades when revenue exists** — Apple Developer ID ($99/yr, removes the Gatekeeper "Open Anyway" scare) → auto-update → Firestore-backed metering.

## Current status (2026-07-21)

**Product complete and distribution built; production hardening in progress** (repo: https://github.com/aseemez/daylook). All 10 build steps done, real capture live-tested (CGWindowList z-order fix; split-screen limitation known). **Tests 78 passed / 6 skipped.** Eval logic unchanged but the Analyst prompt is now v2 (heartbeats), so **the first `eval.py` rerun needs a real `GOOGLE_API_KEY` in `.env`** to repopulate the cache — after that it's free again. Release track done: web chat UI, PyInstaller `.app`/`.dmg` (arm64, unsigned), landing page in `docs/`, and the **relay live on Cloud Run** (`https://contextline-relay-862472415362.us-central1.run.app`, verified answering 2026-07-21; deployed revision predates `/healthz` + the API-key upstream — needs a redeploy). 50 invite tokens in gitignored `relay/tokens.txt`.

Done this session: dev switched to free-tier Gemini API key (`.env` — **owner must paste a real key**), relay gained the `GEMINI_API_KEY` upstream mode, docs rewritten around the production plan, and **hybrid capture shipped** (stable-work heartbeats + switch-only batching caps + Analyst keyframe selection; `kind` field on events, fully backward-compatible with old sidecars/fixtures). Not done: relay redeploy, GitHub release + Pages enablement, Stripe link, app signing, `scripts/archive-real-home.sh` (all outward-facing — wait for the owner's go).

👉 **`PROGRESS.md`** is the detailed handoff; **`DISTRIBUTION.md`** is the ship-and-charge runbook; **`VERIFY.md`** is the owner's self-check list. Real-screen capture stays **consent-gated** — never capture in a new session without an explicit ask. Standing flag: a plaintext `STRIPE_SECRET_KEY` sits in the user's `~/.zshrc`.