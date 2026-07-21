"""Daylook relay — lets invited users share the owner's Gemini access.

The packaged Mac app is pointed here via ``GOOGLE_GEMINI_BASE_URL``, with the
user's invite token riding in the ``x-goog-api-key`` header (that's where the
google-genai SDK puts what it believes is an API key). This service:

  1. checks the token against the invite list (else 401),
  2. locks the model to the cheap allowlist (else 403),
  3. enforces a per-token and a global daily quota (else 429),
  4. forwards the request body untouched to the upstream model service,
     authenticated with the relay's own credentials,
  5. returns the upstream response verbatim (JSON or SSE stream).

Two upstream modes — this is the standard "wrapper SaaS" pattern (server holds
the real provider credential; clients only ever hold app tokens):

  * GEMINI_API_KEY set  -> forward to the Gemini Developer API with that key.
    The production mode: the key lives on a real billing account with provider-
    side quota caps, so cost is bounded even if the relay misbehaves.
  * otherwise           -> forward to Vertex AI with ADC (on Cloud Run: the
    service account). The original mode, billed to the GCP project.

Deliberately simple: in-memory quota counters (run with --max-instances 1;
counters reset on cold start — they are an abuse throttle, not billing truth;
billing truth is the provider dashboard + budget alerts), tokens in an env var
(Secret Manager is the upgrade path). See relay/README.md.

Env vars:
  GEMINI_API_KEY          if set, upstream = Gemini Developer API (production mode)
  GCP_PROJECT             required in Vertex mode; project the calls bill to
  VERTEX_LOCATION         default "global" (this account requires global)
  INVITE_TOKENS           comma-separated valid tokens
  ALLOWED_MODELS          default "gemini-2.5-flash,gemini-2.5-flash-lite"
  RELAY_PER_TOKEN_DAILY   default 250 requests/token/day (UTC)
  RELAY_GLOBAL_DAILY      default 5000 requests/day across everyone
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections import defaultdict

import google.auth
import google.auth.transport.requests
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relay")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
PROJECT = os.environ.get("GCP_PROJECT", "")
LOCATION = os.environ.get("VERTEX_LOCATION", "global")
TOKENS = {t.strip() for t in os.environ.get("INVITE_TOKENS", "").split(",") if t.strip()}
ALLOWED_MODELS = {
    m.strip()
    for m in os.environ.get("ALLOWED_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite").split(",")
}
PER_TOKEN_DAILY = int(os.environ.get("RELAY_PER_TOKEN_DAILY", "250"))
GLOBAL_DAILY = int(os.environ.get("RELAY_GLOBAL_DAILY", "5000"))

# Vertex REST endpoint. location=global uses the plain host (no region prefix).
_VERTEX_HOST = (
    "https://aiplatform.googleapis.com"
    if LOCATION == "global"
    else f"https://{LOCATION}-aiplatform.googleapis.com"
)

app = FastAPI(title="daylook-relay", docs_url=None, redoc_url=None)

# ADC credentials, only needed in Vertex mode: on Cloud Run this is the service
# account, locally it's `gcloud auth application-default login`. Refreshed on expiry.
# In API-key mode we skip ADC entirely so the relay runs anywhere the key does.
_creds = None
if not GEMINI_API_KEY:
    _creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
_http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))

# (token, "YYYY-MM-DD") -> request count. In-memory on purpose (see docstring).
_usage: dict[tuple[str, str], int] = defaultdict(int)


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _bearer() -> str:
    if not _creds.valid:
        _creds.refresh(google.auth.transport.requests.Request())
    return _creds.token


def _google_error(status: int, message: str, status_name: str) -> JSONResponse:
    # Google's error JSON shape — the genai SDK parses this into a proper
    # ClientError instead of choking on an unfamiliar body.
    return JSONResponse(
        status_code=status,
        content={"error": {"code": status, "message": message, "status": status_name}},
    )


@app.get("/healthz")
def healthz() -> dict:
    today = _today()
    return {
        "status": "ok",
        "upstream": "gemini-api-key" if GEMINI_API_KEY else "vertex-adc",
        "tokens": len(TOKENS),
        "usage_today": {
            # redact tokens to a prefix — healthz is public
            f"{tok[:6]}…": n for (tok, day), n in _usage.items() if day == today
        },
    }


@app.post("/v1beta/models/{model_and_method}")
async def proxy(model_and_method: str, request: Request) -> Response:
    # The SDK requests "models/<model>:<method>" — ":" is a legal path char,
    # so it lands here as one segment and we split it ourselves.
    model, _, method = model_and_method.partition(":")
    if method not in ("generateContent", "streamGenerateContent"):
        return _google_error(404, f"unsupported method {method!r}", "NOT_FOUND")

    token = request.headers.get("x-goog-api-key", "")
    if token not in TOKENS:
        return _google_error(401, "invalid invite token", "UNAUTHENTICATED")

    if model not in ALLOWED_MODELS:
        return _google_error(403, f"model {model!r} is not available", "PERMISSION_DENIED")

    # Daily quotas (UTC). Checked before forwarding so over-quota costs nothing.
    today = _today()
    if _usage[(token, today)] >= PER_TOKEN_DAILY:
        return _google_error(429, "daily limit reached — resets at midnight UTC", "RESOURCE_EXHAUSTED")
    if sum(n for (t, day), n in _usage.items() if day == today) >= GLOBAL_DAILY:
        return _google_error(429, "service daily limit reached", "RESOURCE_EXHAUSTED")

    if int(request.headers.get("content-length") or 0) > 25 * 1024 * 1024:
        return _google_error(413, "request too large", "FAILED_PRECONDITION")

    _usage[(token, today)] += 1
    # keep the counter dict from growing forever
    for key in [k for k in _usage if k[1] != today]:
        del _usage[key]

    body = await request.body()
    stream = method == "streamGenerateContent"
    if GEMINI_API_KEY:
        upstream_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{method}"
        )
        headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    else:
        upstream_url = (
            f"{_VERTEX_HOST}/v1beta1/projects/{PROJECT}/locations/{LOCATION}"
            f"/publishers/google/models/{model}:{method}"
        )
        headers = {"Authorization": f"Bearer {_bearer()}", "Content-Type": "application/json"}
    log.info("token=%s… model=%s method=%s bytes=%d", token[:6], model, method, len(body))

    if not stream:
        upstream = await _http.post(upstream_url, content=body, headers=headers)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    # SSE passthrough (used by `adk web`'s streaming mode; the packaged app
    # only calls the non-streaming path).
    req = _http.build_request("POST", upstream_url + "?alt=sse", content=body, headers=headers)
    upstream = await _http.send(req, stream=True)

    async def _relay_bytes():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        _relay_bytes(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )
