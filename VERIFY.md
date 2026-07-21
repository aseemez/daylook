# How to check things yourself

A checklist for verifying Daylook is healthy — the code, the tests, the live relay,
and the finished Mac app. Each check has: **what it proves**, **what to run**, and **what
a good result looks like**. Copy-paste the commands from the repo root
(`/Users/ezies/capstone`).

> First, once per terminal session, turn on the dev environment:
> ```bash
> cd /Users/ezies/capstone
> source .venv/bin/activate
> ```
> If `python` says "command not found", it's because you skipped this line.
>
> **One-time:** live AI runs in dev need a free Gemini key in `.env` — make one at
> <https://aistudio.google.com/apikey> and paste it into `GOOGLE_API_KEY`. Tests and
> cached eval runs work without it.

---

## 1. The automated tests pass

**Proves:** nothing in the code is broken.

```bash
python -m pytest -q
```

**Good result:** the last line says **`78 passed, 6 skipped`** (the 6 skips are optional
live-AI and MCP tests — they're meant to skip). Any `failed` is a real problem.

---

## 2. The evaluation still scores 5/5

**Proves:** the full pipeline turns capture data into a correct, non-hallucinated journal.

```bash
python eval.py
```

**Good result:** each of the 5 criteria prints **PASS** and the overall score is **5/5**.
The first run may make a couple of AI calls; **re-running is free** (it uses the on-disk
cache) and should finish in ~1 second.

---

## 3. The shared-AI relay is live

**Proves:** the Cloud Run relay is up and correctly gate-keeping invite codes.

```bash
RELAY="https://contextline-relay-862472415362.us-central1.run.app"
TOKEN=$(head -1 relay/tokens.txt)   # your first invite code

# A) valid code -> a real Gemini answer
curl -s "$RELAY/v1beta/models/gemini-2.5-flash:generateContent" \
  -H "x-goog-api-key: $TOKEN" -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"say hi"}]}]}' | head -c 300; echo

# B) a fake code -> rejected
curl -s -o /dev/null -w "fake code -> HTTP %{http_code}\n" \
  "$RELAY/v1beta/models/gemini-2.5-flash:generateContent" \
  -H "x-goog-api-key: cl_not_real" -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}'

# C) which upstream is it running? (404 here = old revision, needs a redeploy)
curl -s "$RELAY/healthz"; echo
```

**Good result:** (A) prints JSON with model text; (B) prints **`fake code -> HTTP 401`**;
(C) prints a JSON status including `"upstream"` — `vertex-adc` means it's still billing
the trial project, `gemini-api-key` means it's on the production path. (First call after
idle may take a few seconds while Cloud Run wakes up — normal, it scales to zero.)

---

## 4. The dev pipeline, end to end, in your browser

**Proves:** capture → analyst → journalist → memory → chat all work together — on **fake**
data, safely isolated from your real journal.

```bash
scripts/dev-seed.sh          # writes ~30 fake capture events into .dev_home
scripts/dev-api.sh           # starts the local server (leave this running)
```

Then open **http://127.0.0.1:8000** in your browser and ask: *"what did I do today?"*

**Good result:** you get a timeline (times, labels, apps) — not just a wall of text. All of
this reads/writes `.dev_home` inside the repo; **your real `~/.daylook` is untouched.**
Press `Ctrl-C` in the terminal to stop the server.

---

## 5. The finished Mac app

**Proves:** the actual thing a user downloads works.

1. In Finder, open `dist/Daylook.dmg`, drag **Daylook** onto **Applications**.
2. Open it from Applications. First time, macOS blocks it — go to **System Settings →
   Privacy & Security → scroll down → Open Anyway**, confirm once.
3. It opens in your browser with a setup box. Paste any code from `relay/tokens.txt`, click
   Connect.
4. Ask it something in the chat.

**Good result:** it answers — which means the app reached the live relay using only the
invite code (no keys, no setup). This is a real user's exact experience.

> To test without installing, run the bundled binary against a throwaway folder:
> ```bash
> DAYLOOK_HOME=$(mktemp -d) dist/Daylook.app/Contents/MacOS/Daylook --smoke --no-browser
> ```
> Good result: it prints `smoke: OK — server healthy`.

---

## 6. The code is safely on GitHub

**Proves:** everything is backed up to the remote.

```bash
git fetch origin -q
git status -sb | head -1
```

**Good result:** the line reads **`## main...origin/main`** with **no** "ahead"/"behind".
You can also just open <https://github.com/aseemez/daylook> and see the latest commits.

---

## 7. Cost & usage (peace of mind)

**Proves:** you can see spending before it hurts.

- **While the relay is in `vertex-adc` mode:** the GCP billing dashboard →
  <https://console.cloud.google.com/billing> (you already set a **$290 budget alert**), and
  the Cloud Run service page →
  <https://console.cloud.google.com/run/detail/us-central1/daylook-relay>.
- **Once it's in `gemini-api-key` mode:** the AI Studio / Gemini API usage page for that
  key, plus the quota caps you set on it — those caps are the hard ceiling on spend.

---

## Quick reference

| Thing | Where |
|---|---|
| Live relay URL | `https://contextline-relay-862472415362.us-central1.run.app` |
| Your 50 invite codes | `relay/tokens.txt` (local only — share individual codes, never the file) |
| Built app / installer | `dist/Daylook.app`, `dist/Daylook.dmg` |
| Ship-and-charge runbook | `DISTRIBUTION.md` |
| Project state / handoff | `PROGRESS.md` |
| Your GCP project | `project-73b31b23-b6f9-4055-889` |
