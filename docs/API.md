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
2. **Per-model daily limits** (optional, set per model tag in
   `gateway/app/models_registry.yaml` as `daily_limit: N`) — a rolling
   24-hour cap per API key *per model*, checked only on `POST /v1/jobs`.
   `qwen2.5:7b-instruct-q4_K_M` currently has `daily_limit: 200`. This applies
   across every use of that model by a given key — the gateway has no way to
   tell "why" a model was called, only which one and by whom. Exceeding it
   returns `429` with a message naming the model and limit.

## Two ways to call a model

| | `POST /v1/chat` | `POST /v1/jobs` + `GET /v1/jobs/{id}` |
|---|---|---|
| Style | Synchronous — holds the connection open until done | Asynchronous — returns immediately, poll for the result |
| Use for | Short requests (seconds) | Anything that might run long (minutes) — large payloads, big context |
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

Lists the real Ollama model names currently allowlisted, as defined in
`models_registry.yaml`. Use one of these exact strings as `model` in your
own requests — there's no separate alias to look up.

**Response `200`:**
```json
{
  "models": [
    {"name": "qwen2.5:7b-instruct-q4_K_M", "description": "General-purpose 7B model. Used today for health-data synthesis."},
    {"name": "meditron:7b-q4_K_M", "description": "Optional: domain-tuned model for narrow clinical-terminology Q&A. Only ~4K token context."}
  ]
}
```

---

## `POST /v1/chat`

Synchronous call. Use for short requests; avoid for anything that might run long on real data (see table above).

**Request:**
```json
{
  "model": "qwen2.5:7b-instruct-q4_K_M",
  "messages": [
    {"role": "user", "content": "What's a healthy resting heart rate range?"}
  ]
}
```
`model` must be one of the exact strings returned by `GET /v1/models` — this
is the real Ollama tag, not an alias. `messages` follows the common
`{role, content}` shape (`role` is `"user"`, `"assistant"`, or `"system"`).
The gateway is a pure passthrough — `messages` goes to Ollama exactly as you
sent it, nothing added or rewritten. Include your own `system` message if
you want specific behavior; there is no automatic default.

**Response `200`** (default, `stream: false` or omitted):
```json
{
  "model": "qwen2.5:7b-instruct-q4_K_M",
  "message": {"role": "assistant", "content": "A healthy resting heart rate for most adults is 60-100 beats per minute..."}
}
```

**`stream: true`** — returns `text/event-stream` instead: one `data:` line
per chunk as the model generates it, ending with a literal `data: [DONE]`
line. Each chunk's payload is shaped like Ollama's own streaming chunks:
```
data: {"message": {"role": "assistant", "content": "A"}, "done": false}

data: {"message": {"role": "assistant", "content": " healthy"}, "done": false}

data: {"message": {"role": "assistant", "content": ""}, "done": true, ...}

data: [DONE]
```
If the backend fails partway through, one more `data:` line is sent —
`data: {"error": "Ollama backend error: ..."}` — since the response's `200`
status and headers are already committed by the time streaming starts, an
error can't become an HTTP error status at that point; check for an `error`
key in any chunk. Streaming is only available on `/v1/chat`, not `/v1/jobs`
— see that endpoint's note below for why.

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
  "model": "qwen2.5:7b-instruct-q4_K_M",
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

`model` here is the same real Ollama tag you submitted the job with —
requests and responses both use the same identifier, there's no separate
alias translation happening anywhere. It's `null` only while the job is
still `queued`, since nothing has started running yet to name; it's set the
instant processing begins.

**While queued:**
```json
{
  "job_id": "9a9318ba-...",
  "model": null,
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
  "model": "qwen2.5:7b-instruct-q4_K_M",
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
  "model": "qwen2.5:7b-instruct-q4_K_M",
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
  "model": "qwen2.5:7b-instruct-q4_K_M",
  "status": "failed",
  "created_at": "...",
  "started_at": "...",
  "finished_at": "...",
  "error": "Interrupted by a server restart before this job finished."
}
```
Other `error` values include Ollama backend errors (e.g. a timeout on an unusually large payload) and `"Unknown model '<name>'"` if the registry changed between submission and processing — in that failure case specifically, `model` stays `null` since no real model was ever resolved.

**On cancellation** (see `DELETE` below):
```json
{
  "job_id": "9a9318ba-...",
  "model": "qwen2.5:7b-instruct-q4_K_M",
  "status": "cancelled",
  "created_at": "...",
  "started_at": "...",
  "finished_at": "...",
  "error": "Cancelled by user request."
}
```

**Recommended polling pattern:** poll every 3-5 seconds while `status` is
`queued` or `processing`; stop once it's `done`, `failed`, or `cancelled`.
There is no webhook/callback mechanism — polling is the only option today.
`stream: true` is not supported on `/v1/jobs` — the point of this endpoint is
that the client never holds a connection open, which is the opposite of what
streaming needs; use `POST /v1/chat` with `stream: true` if you want tokens
as they're generated.

---

## `DELETE /v1/jobs/{job_id}`

Cancels a submitted job. Same ownership rule as `GET` — a different API key
gets `404`.

- **`queued` job:** cancelled immediately, nothing was running yet.
- **`processing` job:** actually interrupted — the in-flight request to
  Ollama is aborted (Ollama itself stops generating, it doesn't keep
  computing in the background), and the worker becomes free to start the
  next queued job right away, typically within a second. This returns
  `status: "cancelling"` rather than `"cancelled"`, since the row update to
  `"cancelled"` happens moments later in the background — poll `GET
  /v1/jobs/{job_id}` to confirm.
- **`done` / `failed` / `cancelled` job:** too late, returns `409`.

**Response `200`** (queued job):
```json
{"job_id": "9a9318ba-...", "status": "cancelled"}
```

**Response `200`** (processing job):
```json
{"job_id": "9a9318ba-...", "status": "cancelling"}
```

**Errors:**
| Status | Meaning |
|---|---|
| `401` | Missing/invalid API key |
| `404` | Job doesn't exist, or belongs to a different API key |
| `409` | Job already `done`, `failed`, or `cancelled` — nothing to cancel |

---

## Full example: submit and poll (curl)

```bash
# 1. Submit
JOB=$(curl -s -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:7b-instruct-q4_K_M","messages":[{"role":"user","content":"..."}]}' \
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
