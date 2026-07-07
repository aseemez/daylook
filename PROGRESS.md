# Contextline — Progress & Handoff

> Living status doc for whoever (human or agent) picks this up next. CLAUDE.md is the
> spec / source of truth; **this file is the current state of the world**. Read both.
> Last updated: 2026-07-07 — added `adk web` dev-UI entry point + in-chat start/stop capture control.

## TL;DR

**All 10 build steps are complete, real screen capture is proven LIVE, and the project now also
runs in the `adk web` dev UI with in-chat start/stop of real capture.** The pipeline works end to
end against real Vertex AI (Gemini 2.5 Flash), scores **5/5 on `eval.py`**, serves over HTTP, and
ingests **real screenshots of the user's actual screen**, not just mock data. Tests: **51 passed,
6 skipped** (skips are opt-in live-LLM tests + MCP-needs-npx).

### New since 2026-07-05 (this session's work — all pushed to `main`)
- **`adk web` support** (`2208438`). New package `adk_web/contextline_agent/` exposes a
  module-level `root_agent` (via `RootAgent().build_llm_agent()`) so the ADK dev UI can drive the
  same underlying `LlmAgent` with its own runner/session service. **Run it as `adk web adk_web`
  from the repo root** — NOT bare `adk web` (that lists the code packages `contextline`/`tests`
  too; only `contextline_agent` is the agent). Session refinements work unchanged via
  `tool_context.state`.
- **In-chat start/stop of real capture** (`01f6bb8`). RootAgent gained `start_capture_process` /
  `stop_capture_process` / `capture_status` (subprocess mgmt: spawns `python -m
  contextline.capture_script.real_capture`, graceful **SIGINT** on stop → terminate → kill,
  idempotent). Exposed to the chat as `start_capture(minutes)` / `stop_capture()` tools, so the
  user can say "start tracking my screen" / "stop" from the dev UI. **Still consent-gated** — the
  tool records the real screen; the agent only calls it on a clear request.
- Tests: `tests/test_capture_control.py` (3, hermetic — `subprocess.Popen` is faked, **never
  touches the screen**) cover the start→already_running→stop lifecycle, `--dry-run`/`--minutes`
  command construction, and SIGINT-on-stop.
- `.gitignore`: added `.adk/` (adk web per-session SQLite runtime junk).
- **`WRITEUP.md` exists locally but is deliberately NOT committed** (user's call — it's the
  2498-word Kaggle capstone writeup). It's the only untracked file; leave it that way unless asked.

Step 10 (real macOS capture) is **done, deps installed, and live-tested**. During testing we
found and **fixed a real bug**: frontmost-app detection used `NSWorkspace.frontmostApplication()`,
which returns a *stale* value in a long-running headless script — it reported the launching
Terminal no matter what app the user switched to, so no context switches were detected. Now it
reads the **CGWindowList on-screen z-order** (fresh each poll); a live 2-min capture correctly
logged Safari, VS Code, Simplenote, Weather, and Terminal as distinct blocks. Also added
`--minutes N` (optional auto-stop; omit to run until Ctrl-C).

Repo: https://github.com/aseemez/contextline — `main` clean and **pushed** (latest `2208438`).
The **whole capstone is presentation-ready** (user is showing it to judges).

## Environment (verified working — do not re-litigate)

- Python **3.14** in a venv at the repo. Google ADK **2.3.0** installed.
- **Vertex AI via ADC** (no API keys anywhere). `.env` (gitignored) holds the real values:
  - `GOOGLE_GENAI_USE_VERTEXAI=TRUE`
  - `GOOGLE_CLOUD_PROJECT=project-73b31b23-b6f9-4055-889` (real id lives in `.env`)
  - `GOOGLE_CLOUD_LOCATION=global`  ← **must be `global`**, not `us-central1` (that failed for this account)
- Real Gemini calls confirmed working. First call costs; **all Analyst/Journalist calls are cached
  on disk**, so eval/pipeline reruns against the same fixture are free (verified: rerun ~1s, no API calls).

## What's done (build steps 1–9)

| # | Step | State | Notes |
|---|------|-------|-------|
| 1 | Mock capture generator | ✅ | `capture_script/mock_generator.py`. 30-event realistic dev morning (09:02–10:56), Pillow-rendered synthetic JPEGs — **not** fetched from the internet, fully local. |
| 2 | Capture Agent | ✅ | `agents/capture.py` + `agents/capture_mcp.py`. Deterministic batching (idle-gap/size/span). **Real MCP filesystem read path** (`@modelcontextprotocol/server-filesystem` via npx), verified byte-identical to the plain-filesystem fallback. MCP toggled via `CONTEXTLINE_USE_MCP`. |
| 3 | Analyst Agent | ✅ | `agents/analyst.py`. Only agent that sees images. LLM **only groups/labels**; all facts (times, durations, app_name, source_event_ids) are **derived deterministically in code** → anti-hallucination by construction. Cached vision calls. |
| 4 | Journalist Agent | ✅ | `agents/journalist.py`. LLM narrative (cached, refinement-aware) + deterministic timeline/stats formatting. |
| 5 | Memory Agent + SQLite | ✅ | `agents/memory.py`, `storage/sqlite.py`. Schema per CLAUDE.md (`days`, `blocks` with `source_event_ids JSON`). Idempotent save; ADK function-tools for historical queries. |
| 6 | Root Agent | ✅ | `agents/root.py`. Orchestrates the four; ADK session state for session-scoped refinements; `asyncio.to_thread` to avoid nested-event-loop issues. Now also owns the real-capture subprocess (start/stop/status) + a `build_llm_agent()` factory for external runners (adk web). |
| 7 | Structured logging | ✅ | `observability.py`. `observe()` wraps every agent step (agent/action/input/output/latency) as JSON lines. |
| 8 | `eval.py` | ✅ | Scores all 5 CLAUDE.md criteria — **5/5**. Uses stable `.eval_home/` so cache+db persist and reruns are free. |
| 9 | FastAPI wrapper | ✅ | `api/main.py`. `POST /chat`, `GET /health`. Local-only, no auth (dev). Verified live via uvicorn: /health ok, /chat processed a day and answered a follow-up from memory. |

## What's left

Nothing required. The build order is complete and real capture is proven live.

### Build step 10 — real macOS capture script — DONE + LIVE-TESTED
- `capture_script/real_capture.py`: polls the frontmost app (~1s); on a debounced active-window
  change emits a capture event (`mss` downscaled JPEG + JSON sidecar) into `pending/`. Writes the
  **same on-disk shape** as the mock generator, so nothing downstream changes. Debounce is a pure
  state machine (`advance()`), unit-tested with no screen.
- **Frontmost detection reads CGWindowList z-order**, NOT `NSWorkspace.frontmostApplication()`
  (which was stale in a headless script — see the bug note in TL;DR / the `frontmost()` comment).
  Skips system-UI owners (menubar/dock/control-center) via `_SKIP_OWNERS`.
- **Deps ARE now installed** (`pip install -e ".[capture]"` → `mss`, `pyobjc-core`, `-Cocoa`,
  `-Quartz`). The user consented and we ran it on their real screen. Screen Recording permission
  is already granted on their Mac (verified: `grab_primary()` returns real content, not black).
- CLI: `--minutes N` (auto-stop), `--once` (single event), `--dry-run` (detect switches, no
  screenshot — `pyobjc` only), `--poll`, `--debounce`, `--out`. Default = run until Ctrl-C.
- Tests: `tests/test_real_capture.py` (5) cover debounce, event id/shape, on-disk write contract.
  (The OS-dependent `frontmost()`/`grab_primary()` are not unit-tested — verified live instead.)

**Known limitation (good talking point for judges):** capture is driven by *active-app* changes.
In macOS **split-screen**, two apps share the screen but only one is "frontmost", so the other's
time is attributed to the focused app (its content is still in the screenshot and the Analyst
reads it, but the *block label* is the focused app). Cmd-Tab between **separate full windows** for
a clean multi-app timeline. This is inherent to event-driven-by-focus, not a bug.

Optional future polish (not required): treat window-*title* changes within an app as sub-switches;
a `blocks`-level DB migration test; streaming API responses.

### Demo recipe (the user is presenting to judges)
1. `python -m contextline.capture_script.real_capture --minutes 5` (or no `--minutes`, Ctrl-C to stop).
2. Spend **30–60s per app in separate full windows**, Cmd-Tab between 4–6 apps (short dwell → tiny
   durations; the 2-min test showed "0m" blocks purely because each app got only seconds).
3. Ask the pipeline "What did I do today?" via `/chat` (or the RootAgent) → agents produce the journal.
   NOTE: processing moves events `pending/ → processed/`, so a second "today" query finds nothing —
   re-seed or ask about the saved day if re-demoing.

## Gotchas learned this session (save the next agent the pain)

- **`process_today` blends everything in `pending/`.** If mock events (from `mock_generator.py`)
  and real-capture events both sit in `pending/`, the journal mixes them and looks "half real, half
  fake" — this is NOT hallucination, it's leftover data. **Before a real-capture demo, make sure
  `~/.contextline/pending/` and `processed/` are empty** (and there's no stale journal row for the
  target date in `journal.sqlite`). We hit exactly this and it read as a hallucination bug.
- **App names are ALWAYS from the live macOS window list**, never invented by the model. If an app
  you didn't use shows up (e.g. an old "Weather" block), it came from an *earlier* capture session's
  events still on disk — check `~/.contextline/capture.log` for which session actually logged it
  before blaming the model. Don't delete the evidence before diagnosing.
- Processing moves events `pending/ → processed/`, so a second "what did I do today?" finds nothing
  new. Re-seed or ask about the saved day when re-demoing.

## Decisions already made (don't re-open these)

- **`GOOGLE_CLOUD_LOCATION=global`** — `us-central1` did not work for this account.
- **Anti-hallucination is structural**, not prompt-based: the model never emits times/apps/ids;
  code derives them from real events and cross-checks via `source_event_ids`. Keep it this way.
- **MCP is genuinely wired** (not faked) with a plain-filesystem fallback, per CLAUDE.md's allowance.
- **On-disk cache keyed on exact input batch** — makes eval/pipeline reruns free. Don't remove.
- **Eval criterion 5** (session refinement) probes the day's dominant topic (JWT/auth work), which the
  narrative reliably mentions, rather than "Slack" (which the baseline didn't organically mention).
- **Live-LLM tests are opt-in** via `RUN_LLM_TESTS=1` so the default `pytest` run is hermetic/free.
- Model everywhere is **`gemini-2.5-flash`**; only Analyst gets images; screenshots downscaled to 1024px/q70.

## Decisions still OPEN (need the user)

Nothing blocking. Step 10 is fully resolved (built, deps installed, live-tested, bug fixed,
pushed). Real-screen capture is still **consent-gated** as a principle — the user opted in for the
demo, but don't capture their screen in a future session without an explicit ask. Possible next
asks if the user wants them: a longer real capture for a polished demo timeline, or the optional
polish items above (window-title sub-switches, streaming API).

## Unrelated flags raised (outside the capstone)

- **Status line**: CONFIGURED this session. `~/.claude/statusline-command.sh` shows
  `dir | git-branch | model | ctx N% | $cost | 5h N% | 7d N%` (rate-limit % only shows on Pro/Max).
  Wired in `~/.claude/settings.json`. Any further changes must go through the **statusline-setup**
  agent. (There was no PS1 to convert; built from scratch per the user's ask to surface usage.)
- **⚠️ Security**: `~/.zshrc` line 7 has a **`STRIPE_SECRET_KEY` hardcoded in plaintext**. Flagged to the
  user; recommended moving it to a gitignored `.env` / secrets manager. **Still not actioned** (user's
  call). Also relevant to capture: warned the user not to have it on screen while capturing.

## How to run things (quick reference)

```bash
# from repo root, venv active, .env present
python -m contextline.capture_script.mock_generator   # seed fake events into ~/.contextline/pending
python -m contextline.capture_script.real_capture --minutes 5   # REAL screen capture (macOS, consent-gated)
python eval.py                                         # score the 5 criteria (free after first run)
pytest                                                 # 51 passed, 6 skipped
RUN_LLM_TESTS=1 pytest                                 # include live-model tests (first run costs)
uvicorn contextline.api.main:app --reload             # then POST /chat, GET /health
adk web adk_web                                        # ADK dev UI → pick "contextline_agent" (NOT bare `adk web`)
```

In the dev UI you can chat naturally: "process today", "what did I do today?", a refinement like
"don't count Slack as focus time", or **"start tracking my screen for 5 minutes"** / "stop" to
drive real capture in-chat (consent-gated — records the real screen).

## Working agreement with the user (Aseem)

- Aseem is **non-technical** and wants the agent to **drive** — make decisions, don't ask permission for
  routine work, but **verify** ("check your stuff, don't be overconfident").
- **Consent-gate anything that touches real personal data** (screen capture, secrets). That's the one
  place to stop and confirm.
