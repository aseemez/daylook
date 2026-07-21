# Daylook relay

A tiny proxy that lets invited/paying users run Daylook against the owner's
Gemini access without any credentials shipping in the app. See the module
docstring in `main.py` for what it does; see `DISTRIBUTION.md` at the repo
root for where it fits in the release story.

This is the standard "wrapper SaaS" shape: the server holds the real provider
credential, the client holds only an app token (`cl_...`), and the server
meters + forwards. Users never need a Google account or key.

## Two upstream modes

| Mode | Trigger | Auth upstream | Bills to |
|---|---|---|---|
| **Gemini API key** (production) | `GEMINI_API_KEY` set | the key itself | your Gemini API billing account (set provider-side quota caps!) |
| **Vertex + ADC** (legacy) | `GEMINI_API_KEY` unset | Cloud Run service account | the GCP project (`GCP_PROJECT`) |

The client is identical in both modes — flipping the mode is a redeploy with
one env var, no app rebuild.

## Run locally

```bash
cd relay
pip install -r requirements.txt

# API-key mode (no gcloud needed — works with a free-tier AI Studio key):
GEMINI_API_KEY=<your-key> INVITE_TOKENS=cl_test uvicorn main:app --port 9000

# or Vertex mode (uses your dev ADC):
GCP_PROJECT=<your-project> VERTEX_LOCATION=global INVITE_TOKENS=cl_test \
  uvicorn main:app --port 9000
```

Smoke test:

```bash
curl -s http://127.0.0.1:9000/v1beta/models/gemini-2.5-flash:generateContent \
  -H "x-goog-api-key: cl_test" -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"say hi"}]}]}'
```

## Deploy to Cloud Run (free tier, scales to zero)

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com

python gen_tokens.py 50 > tokens.txt   # keep this file private, never commit

# Production (API-key upstream). Put the key in Secret Manager when convenient;
# an env var is acceptable at this scale:
gcloud run deploy daylook-relay --source . --region us-central1 \
  --allow-unauthenticated --max-instances 1 --min-instances 0 \
  --memory 512Mi --timeout 300 \
  --set-env-vars "GEMINI_API_KEY=<paid-or-free-tier-key>,RELAY_PER_TOKEN_DAILY=250,INVITE_TOKENS=$(paste -sd, tokens.txt)"
```

Legacy Vertex deploy (bills the GCP project; needs `aiplatform.googleapis.com`
enabled and `roles/aiplatform.user` on the service account) — replace the
`--set-env-vars` with `GCP_PROJECT=<project-id>,VERTEX_LOCATION=global,...` and
omit `GEMINI_API_KEY`.

Notes:

- `--max-instances 1` is load-bearing: quota counters are in-memory, one
  instance keeps them authoritative. Counters reset on cold start — they are an
  **abuse throttle, not billing truth**. Billing truth = the provider's usage
  dashboard + a budget alert + provider-side quota caps on the API key. A
  Firestore counter is the upgrade if precise metering ever matters.
- The Cloud Run *region* (us-central1) is independent of the upstream mode.
- Rotate tokens: edit INVITE_TOKENS and redeploy (`gcloud run services update
  daylook-relay --update-env-vars ...`). Keep the private name→token sheet
  so one leaked/lapsed customer token can be dropped without disturbing others.
- `GET /healthz` reports the active upstream mode and today's per-token usage
  (token prefixes only). If the deployed revision 404s on it, the revision
  predates the endpoint — redeploy.
