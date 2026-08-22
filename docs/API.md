# API Reference

Base URL: `http://127.0.0.1:8000` locally, or your Cloudflare Tunnel URL
(a stable `https://api.yourdomain.com` once configured, or a temporary
`https://<random-words>.trycloudflare.com` if you haven't set up a domain
yet — see [`cloudflared/README.md`](../cloudflared/README.md)).

## Authentication

Every endpoint except `GET /healthz` requires an API key, sent as a Bearer token:

```
Authorization: Bearer <your key>
```

Keys are defined in `.env` as `API_KEYS=name:key,name:key,...` — each calling
application should get its own named key so usage can be told apart and one
key can be revoked without affecting others. A missing or invalid key returns
`401 Unauthorized`.

**Keys are bearer credentials, not passwords tied to a user** — whoever holds
a key can use it as that application, with no further identity check. Never
embed a key in client-side code (a web page's JavaScript, a mobile app
bundle) where an end user could extract it from network traffic. Only your
own backend should hold and use these keys, calling this gateway
server-to-server.

## Rate limits

Two independent limits apply:

1. **`RATE_LIMIT_PER_MINUTE`** (default 20) — a short-window cap per API key,
   applied to `POST /v1/chat` and `POST /v1/jobs`. Exceeding it returns `429`.
2. **Per-model daily limits** (optional, set per logical model in
   `gateway/app/models_registry.yaml` as `daily_limit: N`) — a rolling
   24-hour cap per API key *per model*, checked only on `POST /v1/jobs`.
   `insights` currently has `daily_limit: 200`. Exceeding it returns `429`
   with a message naming the model and limit.

## Two ways to call a model

| | `POST /v1/chat` | `POST /v1/jobs` + `GET /v1/jobs/{id}` |
|---|---|---|
| Style | Synchronous — holds the connection open until done | Asynchronous — returns immediately, poll for the result |
| Use for | `chat` (fast, seconds) | `insights` (slow, minutes) or anything that might run long |
| Why | Simpler for short responses | A synchronous call this long is unreliable for most HTTP clients, and is **hard-killed after 100 seconds** by Cloudflare's free-tier tunnel regardless of local settings |

If in doubt, use `/v1/jobs` — it works fine for short requests too, it's just one extra poll.

---

## `GET /healthz`

Unauthenticated liveness probe. Used by Docker's own healthcheck and safe to
call for a quick sanity check.

**Response `200`:**
```json
{"status": "ok", "ollama_reachable": true}
```
`ollama_reachable: false` means the gateway is up but can't reach the Ollama
container — check `docker compose logs ollama`.

---

## `GET /v1/models`

Lists the logical model names currently available, as defined in
`models_registry.yaml`.

**Response `200`:**
```json
{
  "models": [
    {"name": "insights", "description": "Synthesizes lab reports, daily metrics, and medication history into a holistic view. 32K token context — fits weeks of structured data in one request."},
    {"name": "chat", "description": "General health chatbot. Same underlying model as 'insights' today, kept as a separate name so you can swap just one of them later without affecting the other."},
    {"name": "medical_qa", "description": "Optional: domain-tuned model for narrow clinical-terminology Q&A. Only ~4K token context — not suitable for the multi-source data synthesis 'insights' handles; kept here for side-by-side comparison if you want it."}
  ]
}
```

---

## `POST /v1/chat`

Synchronous call. Use for `chat`; avoid for `insights` on real data (see table above).

**Request:**
```json
{
  "model": "chat",
  "messages": [
    {"role": "user", "content": "What's a healthy resting heart rate range?"}
  ]
}
```
`messages` follows the common `{role, content}` shape (`role` is `"user"`,
`"assistant"`, or `"system"`). If the target model has a default
`system_prompt` configured in the registry, it's automatically prepended
**unless** your own `messages` already includes a `system` entry — so you can
always override the default per-request.

`stream` is accepted in the body but not yet implemented — `stream: true`
returns `400 Bad Request`. Omit it or set `false`.

**Response `200`:**
```json
{
  "model": "chat",
  "message": {"role": "assistant", "content": "A healthy resting heart rate for most adults is 60-100 beats per minute..."}
}
```

**Errors:**
| Status | Meaning |
|---|---|
| `401` | Missing/invalid API key |
| `404` | Unknown `model` name — check `GET /v1/models` |
| `422` | Malformed request body (missing fields, wrong types) |
| `429` | Rate limit exceeded |
| `502` | Ollama backend error (including a timeout — see Troubleshooting in `docs/OPERATIONS.md`) |

---

## `POST /v1/jobs`

Asynchronous submission. Same body shape as `/v1/chat`. Returns immediately
with a job ID rather than waiting for the model.

**Request:** identical shape to `/v1/chat`:
```json
{
  "model": "insights",
  "messages": [
    {"role": "user", "content": "Here is my health data as JSON: {...}. What patterns do you see?"}
  ]
}
```

**Response `202 Accepted`:**
```json
{"job_id": "9a9318ba-c123-4e4b-8671-2d1e54e89092", "status": "queued"}
```

**Errors:**
| Status | Meaning |
|---|---|
| `401` | Missing/invalid API key |
| `404` | Unknown `model` name |
| `422` | Malformed request body |
| `429` | Either the per-minute rate limit, or this model's per-key daily limit, was exceeded — the error message tells you which |

---

## `GET /v1/jobs/{job_id}`

Poll for a submitted job's status and, once finished, its result. Only the
API key that submitted a job can view it — a different key gets `404`, the
same as a nonexistent ID (so one application can't confirm another's job IDs
are valid).

**While queued:**
```json
{
  "job_id": "9a9318ba-...",
  "model": "insights",
  "status": "queued",
  "queue_position": 2,
  "created_at": "2026-08-22T15:20:00+00:00",
  "started_at": null,
  "finished_at": null
}
```
`queue_position` is how many other queued jobs are ahead of this one (`0` means next up). Jobs are processed strictly one at a time, in submission order.

**While processing:**
```json
{
  "job_id": "9a9318ba-...",
  "model": "insights",
  "status": "processing",
  "created_at": "2026-08-22T15:20:00+00:00",
  "started_at": "2026-08-22T15:20:04+00:00",
  "finished_at": null
}
```

**On success:**
```json
{
  "job_id": "9a9318ba-...",
  "model": "insights",
  "status": "done",
  "created_at": "...",
  "started_at": "...",
  "finished_at": "2026-08-22T15:28:12+00:00",
  "result": {"message": {"role": "assistant", "content": "### Overall Summary\n..."}}
}
```

**On failure:**
```json
{
  "job_id": "9a9318ba-...",
  "model": "insights",
  "status": "failed",
  "created_at": "...",
  "started_at": "...",
  "finished_at": "...",
  "error": "Interrupted by a server restart before this job finished."
}
```
Other `error` values include Ollama backend errors (e.g. a timeout on an unusually large payload) and `"Unknown model '<name>'"` if the registry changed between submission and processing.

**Recommended polling pattern:** poll every 3-5 seconds while `status` is
`queued` or `processing`; stop once it's `done` or `failed`. There is no
webhook/callback mechanism — polling is the only option today.

---

## Full example: submit and poll (curl)

```bash
# 1. Submit
JOB=$(curl -s -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"model":"insights","messages":[{"role":"user","content":"..."}]}' \
  https://<your-url>/v1/jobs)
JOB_ID=$(echo "$JOB" | jq -r .job_id)

# 2. Poll
while true; do
  STATUS=$(curl -s -H "Authorization: Bearer <key>" "https://<your-url>/v1/jobs/$JOB_ID")
  STATE=$(echo "$STATUS" | jq -r .status)
  echo "status: $STATE"
  if [ "$STATE" = "done" ] || [ "$STATE" = "failed" ]; then
    echo "$STATUS" | jq .
    break
  fi
  sleep 5
done
```

## Data and privacy

This is a personal-scale API. If you're sending real lab results, daily
metrics, or medication history — the intended use — treat model output as a
starting point for noticing patterns, not a diagnosis or treatment plan.
Nothing here is a substitute for a clinician. The gateway does not log
request bodies by default; if you add logging, remember request bodies may
contain sensitive health data and keep any such logs local and out of
version control.
