# Contextline relay

A tiny proxy that lets invited users run Contextline against the owner's
Gemini access without any credentials shipping in the app. See the module
docstring in `main.py` for what it does; see `DISTRIBUTION.md` at the repo
root for where it fits in the release story.

## Run locally (uses your dev ADC)

```bash
cd relay
pip install -r requirements.txt
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
gcloud services enable run.googleapis.com aiplatform.googleapis.com cloudbuild.googleapis.com

python gen_tokens.py 50 > tokens.txt   # keep this file private, never commit

gcloud run deploy contextline-relay --source . --region us-central1 \
  --allow-unauthenticated --max-instances 1 --min-instances 0 \
  --memory 512Mi --timeout 300 \
  --set-env-vars "GCP_PROJECT=<project-id>,VERTEX_LOCATION=global,RELAY_PER_TOKEN_DAILY=250,INVITE_TOKENS=$(paste -sd, tokens.txt)"

# the Cloud Run service account needs permission to call Vertex:
gcloud projects add-iam-policy-binding <project-id> \
  --member serviceAccount:<project-number>-compute@developer.gserviceaccount.com \
  --role roles/aiplatform.user
```

Notes:

- `--max-instances 1` is load-bearing: quota counters are in-memory, one
  instance keeps them authoritative. Counters reset on cold start — fine for
  ~50 trusted users; a Firestore counter is the upgrade if it ever matters.
- The Cloud Run *region* (us-central1) is independent of `VERTEX_LOCATION=global`.
- Rotate tokens: edit INVITE_TOKENS and redeploy (`gcloud run services update
  contextline-relay --update-env-vars ...`).
- Set a **billing budget alert** on the project so credit burn is visible.
