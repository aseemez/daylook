# Daylook

A local work-journal that turns raw screen activity into a semantic daily timeline.

When you spend all day at a computer, the hours blur together and it is hard to remember what you
actually worked on. Daylook watches your screen activity and reconstructs your day as
readable activities, so you can look back, understand your habits, and plan a better tomorrow.

Instead of "Cursor: 3h, Chrome: 1h", the output looks like:

- 09:15–10:00 — Debugging auth flow in Cursor
- 10:00–10:20 — Research on JWT rotation (Chrome, MDN + Stack Overflow)
- 10:20–11:30 — PR review in GitHub

Capture is **hybrid**: an event fires when the active window or app changes (the meaning), plus a
lightweight heartbeat every ~12 seconds while you stay focused on the same thing (the time — so a
two-hour deep-work session counts as two hours, not one context switch). It runs entirely on your
machine, stores everything locally, and exposes a small HTTP API. You talk to it through a chat
interface, asking about the current day, past days, or how you want activity grouped.

## Status

**Complete and running.** The full pipeline works end to end against real Gemini 2.5 Flash,
scores **5/5 on `eval.py`**, and serves over HTTP. Test suite: **78 passed, 6 skipped** (the
skips are opt-in live-model tests plus one MCP case that needs `npx`). There's also a
distribution track — a packaged Mac `.app`, a Cloud Run relay so invited users need only an
invite code, and a landing page — documented in [`DISTRIBUTION.md`](./DISTRIBUTION.md).

All ten build steps from [`CLAUDE.md`](./CLAUDE.md) are implemented, including the real macOS
capture script. See [Real screen capture](#real-screen-capture-macos) for the consent note, and
[`PROGRESS.md`](./PROGRESS.md) for the detailed per-step handoff.

## How it works

![Daylook architecture: a dumb capture layer feeds a pipeline of four agents (Capture, Analyst, Journalist, Memory) that the Root Agent orchestrates and the user chats with](architecture.png)

Daylook has two layers, kept strictly separate.

**The capture layer is deliberately simple.** It records what is on screen and nothing more.
[`real_capture.py`](daylook/capture_script/real_capture.py) writes a capture event — a
screenshot plus a small JSON file with the app name, window title, timestamp, and kind — on every
debounced window switch, and as a stable-work heartbeat every ~12s in between. That is the entire
layer — an event source that the agents read from. (Cost stays flat: the Analyst sends at most ~12
keyframe screenshots per batch to the model; heartbeats mostly travel as text.)

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

Python 3.11+ · Google ADK · Gemini 2.5 Flash (free-tier API key in dev) · SQLite ·
MCP filesystem server (with a plain-tool fallback) · pytest

## Run it yourself

This walks through capturing your own real screen activity and turning it into a journal you can
chat with. You need macOS, Python 3.11+, and a free Gemini API key.

### 1. Get the code

```bash
git clone https://github.com/aseemez/daylook.git
cd daylook
```

Open the folder in VS Code (or your editor) so you can read the agents in `daylook/agents/`
while the system runs.

### 2. Create a virtual environment and install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[capture,dev]"
```

This installs the agent pipeline, the real-capture dependencies (`mss` + `pyobjc`, macOS-only), and
the test tools. It also puts the `adk` command on your PATH, which you'll use in step 6.

### 3. Connect it to Gemini

Get a **free** API key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
(no billing, no cloud project — about 30 seconds), then:

```bash
cp .env.example .env      # then paste your key into GOOGLE_API_KEY
```

The free tier's daily limits are plenty here: the on-disk response cache means repeated runs on
the same data cost no requests at all.

### 4. Let macOS see your screen

Real capture reads the frontmost window and takes screenshots, so grant your terminal (or VS Code)
**Screen Recording** permission under System Settings → Privacy & Security → Screen Recording, then
restart it. Without this, capture still runs but window titles come back empty.

### 5. Capture a real work session

Each context switch — and a heartbeat every ~12s of stable work (`--heartbeat 0` disables) — writes
a downscaled screenshot plus a JSON sidecar into `~/.daylook/pending/`.

```bash
# Safe first run — prints each app/window switch, writes nothing:
python -m daylook.capture_script.real_capture --dry-run

# Capture for 15 minutes, then stop on its own:
python -m daylook.capture_script.real_capture --minutes 15

# Or capture until you press Ctrl-C:
python -m daylook.capture_script.real_capture
```

Work normally across a couple of apps for a few minutes so there's something to summarize. Tip: use
separate full-screen windows and Cmd-Tab between them — in split screen, both apps' time is
attributed to whichever one is focused.

### 6. Build the journal and query it from the terminal

Talk to the whole system right in your terminal with `adk run`, pointing it at the agent:

```bash
adk run adk_web/daylook_agent      # run from the repo root; starts an interactive chat
```

Then just type to it, one line at a time:

- **`process today`** — runs the pipeline over what you captured and writes today's journal
- **`what did I do today?`** — shows the timeline back to you
- **`how much time did I spend in Cursor?`** — asks about the day you just built
- **`don't count Slack as focus time`** — a refinement it honors for the rest of the chat

You can also fire a single question without entering the chat:

```bash
adk run adk_web/daylook_agent "process today"
adk run adk_web/daylook_agent "what did I work on this morning?"
```

### 7. Or use the browser dev UI

Prefer a graphical chat? The same agent runs in ADK's web UI:

```bash
adk web adk_web            # run from the repo root, then open the URL it prints
```

Pick **daylook_agent** in the sidebar and use the same prompts as above.

## Tests and evaluation

```bash
python eval.py            # scores the 5 criteria against a fixed fixture (free after the first run)
pytest                    # 78 passed, 6 skipped — hermetic, no API calls
RUN_LLM_TESTS=1 pytest    # include the live-model tests (first run costs)
```
