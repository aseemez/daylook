# Distributing & charging for Daylook

How the app gets from this repo to a paying user's Mac. Four moving parts:

1. **The app** — a PyInstaller `.app` inside a `.dmg`, downloaded from a GitHub
   Pages landing page and served as a GitHub Release asset.
2. **The relay** — a Cloud Run proxy holding the real Gemini credential, so
   users share the owner's AI access without any credentials shipping in the
   app (`relay/`).
3. **Invite tokens** — one per customer, entered once in the setup card. The
   token is the *only* thing a user needs; it is not a Google key.
4. **The Stripe Payment Link** — how money comes in (see "How this makes money").

---

## How this makes money (the wrapper-SaaS pattern)

Every "GPT wrapper" product works the same way, and Daylook already has the
architecture for it:

```
customer pays (Stripe) ──► gets an app token ──► app sends token to YOUR server
                                                  ──► server calls the AI with
                                                      YOUR provider key
                                                  ──► you pocket (subscription − API cost)
```

The provider key never leaves the server; the customer authenticates to *you*,
not to Google.

**Pricing — paid from day 1, no free tier.** Three plans, all as **Stripe
Payment Links** (Stripe-hosted checkout URLs created in the dashboard with zero
code — one recurring Price each, three links on the landing page):

| Plan | Price | Stripe setup |
|---|---|---|
| Weekly | **$5 / week** | recurring Price, weekly interval |
| Monthly | **$20 / month** | recurring Price, monthly interval |
| Yearly | **$16 / month billed yearly ($192/yr)** | recurring Price, yearly interval, $192 |

The margin holds because capture is engineered for it: hybrid capture (switch
events + 12s stable-work heartbeats) with keyframe thinning means a full
tracked day costs roughly **$1–3/user/month** in Gemini Flash — against $16–20
of revenue — and provider-side quota caps bound the worst case.

**The funnel is built and lives in `web/`** (deploys to Vercel Hobby, free —
runbook in `web/README.md`): an Umax-style onboarding — hook with the scary
numbers (2h48m real focus/day, 23min refocus cost, 47 switches/day) → 3-question
quiz → "analyzing" beat → personalized "you lose ~N hrs/week" reveal → the three
plans → **Stripe Checkout** (serverless function, inline prices — nothing to
configure in the dashboard) → success page that verifies payment server-side and
shows the invite code + download + 4 install steps. In **test mode**
(`sk_test_...` key, card `4242 4242 4242 4242`) this is the complete end-user
journey with no real money — the dress rehearsal for launch; swapping to the
live key is the only change.

**Fulfillment:** the funnel hands out tokens automatically from
`INVITE_TOKEN_POOL` after verified payment. When someone cancels (Stripe emails
you), remove their token from the relay's `INVITE_TOKENS` and redeploy. Minutes
per month at n≈10.

Scale-ups, only when needed: a Stripe webhook that auto-mints and emails tokens
(n≈100), or a merchant-of-record like Paddle if international sales tax becomes
real. The app never changes — it already speaks "token → relay".

---

## 1. Build the Mac app

Built in a dedicated `uv` env on **Python 3.13.7** (PyInstaller against 3.14 is
unverified — don't fight it) with `google-adk` / `google-genai` pinned to the
versions the repo runs on.

```bash
bash packaging/build.sh      # -> dist/Daylook.app  (+ smoke test on a throwaway home)
bash packaging/make_dmg.sh   # -> dist/Daylook.dmg
```

`build.sh` runs the bundle with `--smoke --no-browser` against a `mktemp` home
before it's considered good. Neither `dist/` nor the build venv is committed.

**No `.env` ships.** The launcher owns the environment; `config.py` skips
`load_dotenv()` when frozen so a stray `.env` on the build machine can't leak
dev routing into the bundle.

### Apple Silicon only, unsigned (v1)

The spec targets `arm64`. The app is **unsigned** — first launch needs the
Gatekeeper "Open Anyway" walkthrough documented on the landing page. **The
first revenue pays for the $99/yr Apple Developer ID**: signing + notarization
removes the scariest onboarding step for paying customers and changes nothing
in the code, only the build.

---

## 2. Deploy the relay

Full runbook in **`relay/README.md`**. The relay has two upstream modes;
**production is API-key mode**:

```bash
cd relay
python gen_tokens.py 50 > tokens.txt          # private; never committed (gitignored)
gcloud run deploy daylook-relay --source . --region us-central1 \
  --allow-unauthenticated --max-instances 1 --min-instances 0 \
  --set-env-vars "GEMINI_API_KEY=<key>,RELAY_PER_TOKEN_DAILY=250,INVITE_TOKENS=$(paste -sd, tokens.txt)"
```

- **Testing / pre-revenue:** `GEMINI_API_KEY` can be a **free-tier AI Studio
  key** — $0, daily-capped, good enough to onboard the first testers.
- **Paying customers:** switch that env var to a key on a **paid billing
  account**, and set **provider-side quota caps** on the key plus a billing
  budget alert. That combination — not the relay's counters — is what bounds
  your spend.
- **Legacy:** the currently-deployed revision uses Vertex + the Cloud Run
  service account's ADC, billed to the GCP trial project. It still works, but
  dies when trial credits do — redeploy onto API-key mode before charging
  anyone.

### Point the app at the deployed relay

The live relay is baked into the client as `daylook/settings.py` →
`DEFAULT_RELAY_URL`:

```
https://contextline-relay-862472415362.us-central1.run.app
```

So the packaged app works with nothing but an invite token. Override
per-machine with `DAYLOOK_RELAY_URL`. If you redeploy under a new URL,
update that constant and **rebuild the `.app`** — the URL is compiled in.

### Rotating / revoking tokens

Edit `INVITE_TOKENS` and redeploy (`gcloud run services update
daylook-relay --update-env-vars INVITE_TOKENS=...`). Keep the private
name→token sheet so one customer's token can be dropped (cancellation, leak)
without disturbing anyone else. Quota counters are in-memory and reset on cold
start — an abuse throttle, not billing truth; a Firestore counter is the
upgrade if precise metering ever matters.

---

## 3. Cut a release

```bash
gh release create v0.1.0 dist/Daylook.dmg \
  --title "Daylook v0.1.0" --notes "First public build."
```

The landing page's Download button always points at
`releases/latest/download/Daylook.dmg`, so a new release with that asset
name updates the download with no page edit.

The public front door is the **`web/` funnel on Vercel** (see `web/README.md`);
its success page points at `releases/latest/download/Daylook.dmg`. The old
static landing page (`docs/` on GitHub Pages) is superseded — keep it as a plain
fallback download page or retire it.

---

## 4. Onboarding a customer

1. They pay via the Stripe link (or you comp them) → you email their invite token.
2. Landing page → **Download for Mac**, drag the app to Applications.
3. First open → **System Settings → Privacy & Security → Open Anyway** (until signed).
4. The app opens in the browser; paste the **invite code** into the setup card.
5. Say "start tracking" → grant **Screen Recording** when macOS asks.

Nothing else to configure — no Google account, no key of their own. (A user who
*wants* to bring their own free AI Studio key can: `own_key` mode in
`settings.py`. Zero cost to you, but never make it the required path.)

---

## 5. The "it just works" bar (Cursor-grade smoothness)

People paying $20/mo expect Cursor-level polish: pay → download → paste code →
it works, forever, with zero fiddling. Checklist, in priority order:

1. **Relay on a paid API key with quota caps** — the current trial-credit
   upstream dies without warning; that's the #1 "it stopped working" risk. ✅
   seam built, redeploy pending.
2. **Sign + notarize the app** ($99/yr Apple Developer ID) — removes the
   "macOS blocked this app" scare, which reads as *broken* to a paying user.
   First revenue pays for this.
3. **Kill cold starts** — `--min-instances 1` on Cloud Run (~$5–15/mo) once
   there are paying users, so the first chat of the day doesn't hang 5 seconds.
4. **Capture that recovers** — launcher auto-restarts the capture process if it
   dies; capture starts automatically on app launch once the user has onboarded
   (consent = the macOS Screen Recording grant itself).
5. **Auto-update** (e.g. Sparkle or a launcher self-check against the latest
   release) — nobody re-downloads a `.dmg` to get fixes.
6. **A status surface** — the web UI shows "tracking · N events today · AI
   reachable", so "is it working?" never needs a terminal.

(1–2 are the launch gate for charging; 3–6 land in the first weeks after.)

## 6. Cost & platform roadmap

- **Now:** relay on free/cheap Gemini Flash, three Stripe Payment Links, manual
  tokens. Good through the first ~10 customers.
- **First revenue:** paid Gemini key with quota caps; Apple Developer ID.
- **Growth (only when real):** Stripe webhook → auto token issuance; Firestore
  metering; auto-update; **Windows port** — a new capture script
  (`capture_script/` is the only macOS-specific layer) + a PyInstaller `.exe`;
  the agents, relay, and UI are already cross-platform.
