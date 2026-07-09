# Distributing Contextline

How the app gets from this repo to a user's Mac, for $0. Three moving parts:

1. **The app** — a PyInstaller `.app` inside a `.dmg`, downloaded from a GitHub
   Pages landing page and served as a GitHub Release asset.
2. **The relay** — a tiny Cloud Run proxy so invited users share the owner's
   Gemini access without any credentials shipping in the app (`relay/`).
3. **Invite tokens** — one per user, entered once in the setup card. The token
   is the *only* thing a user needs; it is not a Google key.

Everything here is free-tier: GitHub Pages + Releases cost nothing, Cloud Run
scales to zero, and Gemini runs on the owner's trial credits.

---

## 1. Build the Mac app

Built in a dedicated `uv` env on **Python 3.13.7** (PyInstaller against 3.14 is
unverified — don't fight it) with `google-adk` / `google-genai` pinned to the
versions the repo runs on.

```bash
bash packaging/build.sh      # -> dist/Contextline.app  (+ smoke test on a throwaway home)
bash packaging/make_dmg.sh   # -> dist/Contextline.dmg
```

`build.sh` runs the bundle with `--smoke --no-browser` against a `mktemp` home
before it's considered good, so a broken import fails the build instead of the
user. Neither `dist/` nor the build venv is committed (both gitignored).

**No `.env` ships.** The launcher owns the environment; `config.py` deliberately
skips `load_dotenv()` when frozen so a stray `.env` on the build machine can't
leak Vertex routing into the bundle.

### Apple Silicon only (v1)

The spec targets `arm64`. Intel Macs are out of scope for this release. The app
is **unsigned** — there's no $99 Apple Developer ID yet — so first launch needs
the Gatekeeper "Open Anyway" walkthrough documented on the landing page. Signing
+ notarization is the "when there's money" Mac upgrade; it changes nothing in the
code, only the build.

---

## 2. Deploy the relay

Full runbook in **`relay/README.md`**. Short version:

```bash
cd relay
python gen_tokens.py 50 > tokens.txt          # private; never committed (gitignored)
gcloud run deploy contextline-relay --source . --region us-central1 \
  --allow-unauthenticated --max-instances 1 --min-instances 0 \
  --set-env-vars "GCP_PROJECT=<project-id>,VERTEX_LOCATION=global,\
RELAY_PER_TOKEN_DAILY=250,INVITE_TOKENS=$(paste -sd, tokens.txt)"
gcloud projects add-iam-policy-binding <project-id> \
  --member serviceAccount:<project-number>-compute@developer.gserviceaccount.com \
  --role roles/aiplatform.user
```

Then **set a GCP billing budget alert** on the project (50/90/100% of a small
monthly cap) so credit burn is visible before it hurts.

### Point the app at the deployed relay

The live relay is baked into the client as `contextline/settings.py` →
`DEFAULT_RELAY_URL`:

```
https://contextline-relay-862472415362.us-central1.run.app
```

So the packaged app works with nothing but an invite token. Override per-machine
with `CONTEXTLINE_RELAY_URL` (e.g. to point a dev build at a staging relay). If
you redeploy under a new URL, update that constant and **rebuild the `.app`** —
the URL is compiled into the bundle, not read from a config file at runtime.

### Rotating / revoking tokens

Edit `INVITE_TOKENS` and redeploy (`gcloud run services update contextline-relay
--update-env-vars INVITE_TOKENS=...`). Keep the private name→token sheet so a
leaked token can be dropped without disturbing anyone else. Counters are
in-memory and reset on cold start — fine for ~50 trusted users; a Firestore
counter is the upgrade if it ever matters.

---

## 3. Cut a release

```bash
gh release create v0.1.0 dist/Contextline.dmg \
  --title "Contextline v0.1.0" --notes "First public build."
```

The landing page's Download button always points at
`releases/latest/download/Contextline.dmg`, so a new release with that asset name
updates the download with no page edit.

Enable **GitHub Pages**: repo Settings → Pages → Deploy from `main` branch,
`/docs` folder. `docs/.nojekyll` keeps the page verbatim. It lands at
`https://aseemez.github.io/contextline/`. Hand this link to the first ~50 users
along with their personal invite token.

---

## 4. Onboarding a user (what they do)

1. Open the landing page, click **Download for Mac**, drag the app to Applications.
2. First open → **System Settings → Privacy & Security → Open Anyway** (unsigned).
3. The app opens in the browser; paste the **invite code** into the setup card.
4. Say "start tracking" → grant **Screen Recording** when macOS asks.

That's it. Nothing to configure, no Google account, no key of their own.

---

## 5. Money-later migration (documented, no rewrite)

The `api_key_mode` seam in `settings.py` means "how a user reaches a model" is a
setting, not a code change:

- **User brings their own free AI Studio key** → `own_key` mode: same
  `GOOGLE_API_KEY` seam, no relay indirection, calls go straight to Google on
  their quota. Zero owner cost.
- **Owner funds usage** → relay stays exactly as-is; raise `RELAY_PER_TOKEN_DAILY`
  and/or rotate in more tokens.
- **A paid product later** → put real accounts + billing in front of the relay.
  The app already speaks "token → relay"; only the relay's auth changes.

So users never need "a different AI" — they need a different *token or key*, and
that seam exists today.
