# Project Structure & Developer Reference

A map of the codebase, what each piece does, how a request actually flows
through it, and a condensed log of the decisions and experiments that shaped
it. For the full narrative reasoning behind each decision, see
[`TECHNICAL_OVERVIEW.md`](../TECHNICAL_OVERVIEW.md); for the complete
experiment write-up with raw numbers, see the case study document produced
alongside this project (`Private_AI_Gateway_Case_Study.docx`).

## Directory tree

```
private-ai-gateway/
├── docker-compose.yml          # the 4-container stack: ollama, gateway, webui, cloudflared
├── docker-compose.tunnel.yml   # override: switches cloudflared to a stable named tunnel
├── .env.example                # documented template — copy to .env, never commit .env
├── start.ps1 / stop.ps1        # lifecycle scripts, Windows (see docs/OPERATIONS.md)
├── start.sh / stop.sh          # same lifecycle scripts, Mac/Linux
├── LICENSE                     # MIT
├── README.md                   # quickstart
├── TECHNICAL_OVERVIEW.md       # why every design decision was made
├── docs/
│   ├── API.md                  # this API's external reference
│   ├── OPERATIONS.md           # commands + troubleshooting
│   └── PROJECT_STRUCTURE.md    # this file
├── cloudflared/
│   └── README.md               # one-time Cloudflare account/domain setup
└── gateway/
    ├── Dockerfile               # non-root user, /data pre-owned for the job DB
    ├── requirements.txt
    └── app/
        ├── main.py              # FastAPI app, lifespan (DB init, worker startup), CORS, /healthz
        ├── config.py            # typed settings (Settings/get_settings), reads .env
        ├── auth.py              # require_api_key — Bearer check, timing-safe compare
        ├── rate_limit.py        # slowapi Limiter, keyed by API key not IP
        ├── ollama_client.py     # talks to Ollama's HTTP API; registry loading; streaming; warm-up
        ├── job_store.py         # SQLite-backed async job queue (the persistence layer)
        ├── job_worker.py        # single-consumer background loop that runs and can cancel jobs
        ├── check_pending_jobs.py  # standalone script stop.ps1 uses to warn about unfinished jobs
        ├── models_registry.yaml  # allowlist: real ollama tag -> description + prompt + daily limit
        └── routes/
            ├── models.py        # GET /v1/models
            ├── chat.py          # POST /v1/chat (synchronous)
            └── jobs.py          # POST /v1/jobs, GET /v1/jobs/{id} (asynchronous)
```

## Module-by-module

**`config.py`** — a single `Settings` (pydantic-settings) object reading from
`.env`, cached via `@lru_cache` so it's parsed once. Every other module calls
`get_settings()` rather than reading environment variables directly. Notable
fields: `ollama_num_thread` and `ollama_num_ctx` exist specifically because
Ollama doesn't pick good defaults for a containerized, resource-limited
deployment on its own (see TECHNICAL_OVERVIEW.md §4).

**`auth.py`** — `require_api_key`, a FastAPI dependency used on every
authenticated route. Parses the `Authorization: Bearer` header, compares
against `settings.api_keys_by_secret` using `hmac.compare_digest` (not `==`)
to avoid a timing side-channel, and returns the caller's app name (the
`name` half of `name:key` in `API_KEYS`) for use in logging/limiting/job
ownership checks downstream.

**`rate_limit.py`** — wraps `slowapi.Limiter` with a custom key function that
extracts the Bearer token itself rather than the client IP (every caller
arrives through the same tunnel, so IP-based limiting would be meaningless).
`dynamic_chat_limit()` reads `RATE_LIMIT_PER_MINUTE` from settings at call
time, not import time, so changing `.env` and restarting picks it up.

**`ollama_client.py`** — the only module that talks to Ollama's HTTP API.
Two things live here:
- `load_registry()` / `resolve_model_entry()` — reads `models_registry.yaml`
  fresh on every call (no caching), so registry edits take effect without a
  gateway restart. Used purely as an allowlist check — a model must be
  listed to be callable.
- `chat()` — the actual POST to `/api/chat`, passing `messages` through
  unmodified (the gateway is a pure passthrough — no prompt injection
  anywhere) along with `num_thread`/`num_ctx` explicitly (see
  TECHNICAL_OVERVIEW.md §4 for why Ollama's own defaults aren't trustworthy
  here) and a 1200-second timeout sized for large real-world payloads.
- `chat_stream()` — the same request with Ollama's own `stream: true`, an
  `AsyncIterator` yielding one parsed JSON chunk at a time (Ollama emits
  newline-delimited JSON here, not SSE — `routes/chat.py` is what wraps this
  into actual SSE for the caller).
- `warm_up_first_model()` — sends one throwaway `chat()` call to the first
  model listed in the registry. Fired as an un-awaited background task from
  `main.py`'s lifespan (never blocks startup), and deliberately warms only
  the *first* registry entry: `OLLAMA_MAX_LOADED_MODELS=1` means warming a
  second model would just evict the first, wasting the work.

**`job_store.py`** — all persistence for the async job system, backed by a
single SQLite table (`jobs`) on the `gateway_data` volume. Every function
comes in a `_sync` (plain `sqlite3`, blocking) and an async wrapper
(`asyncio.to_thread(...)`) so the blocking C extension never stalls the
event loop that's also serving HTTP requests. Key functions: `create_job`
(stores the real Ollama tag directly in the `model` column — requests
address models by tag, not an alias, so it's known and correct from the
moment of submission, no later resolution step needed), `claim_next_job`
(atomically picks the oldest queued job and marks it processing — only ever
called from the one worker loop, so no race to guard against), `finish_job`,
`queue_position`, `count_recent_jobs` (backs per-model daily limits), and
`mark_stale_processing_failed` (run once at startup — see "Job lifecycle"
below).

**`job_worker.py`** — `run_worker_loop()`, an `asyncio` task started from
`main.py`'s lifespan hook. A simple claim-process-repeat loop, deliberately
single-consumer: Ollama itself only processes one request at a time
(`OLLAMA_NUM_PARALLEL=1`), so running more than one job concurrently from the
gateway side would just mean multiple of our own requests piling up inside
Ollama's own opaque internal queue instead of ours, breaking the accuracy of
`queue_position`. Each job's `_process_job()` call runs as its own child
`asyncio.Task` (tracked in `_current_job_id`/`_current_job_task`), specifically
so `cancel_current_job(job_id)` — called from `routes/jobs.py`'s `DELETE`
handler, a completely different asyncio context — can cancel just that one
job's task without touching the outer while-loop task. Cancelling raises
`asyncio.CancelledError` inside the `httpx` call to Ollama, which closes that
connection; Ollama's own server (`llama.cpp`) detects the dropped connection
and logs `srv stop: cancel task` — confirmed live, not assumed — so it
actually stops generating rather than finishing in the background. That's
what lets the loop claim the next queued job within milliseconds instead of
waiting behind an orphaned generation still holding Ollama's one processing
slot.

**`routes/chat.py`** — `POST /v1/chat`. Validates the model exists, then
either calls `ollama_client.chat()` and waits for the full result, or (if
`stream: true`) returns a `StreamingResponse` that wraps
`ollama_client.chat_stream()`'s newline-delimited JSON chunks into
Server-Sent Events (`data: {...}\n\n`, ending with `data: [DONE]\n\n`).
`messages` is forwarded unmodified either way. Rate-limited via the
`slowapi` decorator (checked before the handler body runs, so it applies the
same way to both streaming and non-streaming calls).

**`routes/jobs.py`** — `POST /v1/jobs` (validates the model, enforces the
model's `daily_limit` if configured via `job_store.count_recent_jobs`, then
just inserts a row and returns `202` — the actual work happens later, in the
worker loop), `GET /v1/jobs/{id}` (looks up the row, checks the caller owns
it, shapes the response based on `status`), and `DELETE /v1/jobs/{id}` (a
`queued` job is cancelled via `job_store.cancel_if_queued()`'s atomic
compare-and-set; a `processing` job is cancelled via
`job_worker.cancel_current_job()` — see above for what that actually does
under the hood. A job already `done`/`failed`/`cancelled` returns `409`).

**`main.py`** — assembles the FastAPI app: CORS, the rate-limit exception
handler, all three routers, and the `/healthz` endpoint. Also calls
`logging.basicConfig()` at import time — without it, `logger.info`/`warning`
calls anywhere in the app (warm-up status, job failures) are silently
dropped, since Python's root logger defaults to `WARNING` with no handler and
uvicorn only wires up handlers for its own loggers, not arbitrary module
ones. The `lifespan` context manager is where async-job infrastructure
actually starts up: DB init, failing any orphaned `processing` jobs, spawning
the worker task, and firing `warm_up_first_model()` as an un-awaited
background task (see below) so a slow/failing warm-up can't delay `/healthz`
reporting ready.

**`models_registry.yaml`** — not code, but functionally the most important
file for extending this project. Keyed directly by the real Ollama tag (what
callers put in the request's `model` field) — it's an allowlist (a model
must be listed to be callable at all) plus one optional field: `daily_limit`
(per-key, rolling-24h cap, enforced only on `/v1/jobs`, applied per physical
model — two different use cases sharing one model share one limit). Adding a
model is edit-this-file-and-pull, no Python changes. The gateway itself
never adds or rewrites anything in a request's `messages` — there is no
default-behavior/system-prompt injection anywhere; callers get exactly what
they send.

## Workflows

### Synchronous request (`/v1/chat`)

```
client → gateway: POST /v1/chat {model, messages}
  gateway: require_api_key (401 if invalid)
  gateway: rate limit check (429 if exceeded)
  gateway: resolve_model_entry(model) (404 if unknown)
  gateway → ollama: POST /api/chat {tag, messages, options}   # messages unmodified
    ollama: load model if not already warm, run inference
  ollama → gateway: {message: {role, content}}
gateway → client: 200 {model, message}
```
One HTTP connection held open for the entire duration — fine for a short
request (seconds), unworkable for a large real-data payload (minutes) once
anything sits between the client and gateway with its own timeout (browsers,
mobile clients, and specifically Cloudflare's free-tier 100-second proxy limit).

### Asynchronous request (`/v1/jobs`)

```
client → gateway: POST /v1/jobs {model, messages}
  gateway: require_api_key, resolve_model_entry, daily_limit check
  gateway → job_store: create_job(...)  [status=queued]
gateway → client: 202 {job_id, status: "queued"}          <-- returns immediately

  [independently, in the background:]
  job_worker loop: claim_next_job()      [status=queued -> processing]
  job_worker: resolve_model_entry(job["model"])   # job["model"] IS the real tag already
  job_worker: ollama_client.chat(job["model"], messages)   # messages unmodified
  job_worker → job_store: finish_job(...) [status=processing -> done/failed]

client → gateway: GET /v1/jobs/{id}   (polled repeatedly)
gateway → job_store: get_job(id), queue_position(id) if still queued
gateway → client: 200 {status, queue_position | result | error}
```
The submit call and the actual model call are fully decoupled — the client
never holds a long connection open, and a burst of submissions queues
visibly (`queue_position`) instead of silently piling up or timing out.

### Cancelling a job (`DELETE /v1/jobs/{id}`)

```
client → gateway: DELETE /v1/jobs/{id}

  if status == "queued":
    gateway → job_store: cancel_if_queued(id)   # atomic UPDATE...WHERE status='queued'
    if it applied: 200 {status: "cancelled"}    # done, nothing was ever running
    if it didn't (worker just claimed it): fall through to the processing case below

  if status == "processing":
    gateway → job_worker: cancel_current_job(id)
      job_worker: _current_job_task.cancel()    # only if id matches the job actually in flight
        -> raises CancelledError inside _process_job's `await ollama_chat(...)`
        -> httpx closes the connection to Ollama
        -> Ollama detects the drop, logs "srv stop: cancel task", stops generating
        -> _process_job's except CancelledError: job_store.finish_job(id, "cancelled")
      run_worker_loop's while-loop was never itself cancelled, so it immediately
      claims the next queued job — no delay waiting for the aborted generation
    gateway → client: 200 {status: "cancelling"}  # DB row updates to "cancelled" moments later

  if status in (done, failed, cancelled): 409 — nothing to cancel, already finished
```
Measured live: cancelling a `processing` job and having the *next* queued job
go from claimed to fully done took under a second for a trivial prompt — the
worker doesn't wait on Ollama's abandoned generation at all, which is the
whole point of interrupting it rather than just marking it cancelled locally.

### Startup sequence (`main.py` lifespan)

```
1. job_store.init_db()                       — create the jobs table if absent
2. job_store.mark_stale_processing_failed()  — fail anything orphaned by a prior crash/restart
3. asyncio.create_task(run_worker_loop())    — start claiming queued jobs
4. asyncio.create_task(warm_up_first_model()) — fire-and-forget, not awaited
5. (app now serving requests — /healthz doesn't wait on warm-up)
...
6. (on shutdown) worker_task.cancel(); warmup_task.cancel()
```

## Data model (`jobs` table)

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT (UUID) | Primary key, returned to the client as `job_id` |
| `caller` | TEXT | App name from the API key, used for ownership checks and daily-limit counting |
| `model` | TEXT | The exact Ollama tag requested (e.g. `"qwen2.5:7b-instruct-q4_K_M"`) — known and correct from the moment of submission, since requests address models directly by tag |
| `messages` | TEXT (JSON) | The original request body's messages, as submitted |
| `status` | TEXT | `queued` \| `processing` \| `done` \| `failed` \| `cancelled` |
| `result` | TEXT (JSON), nullable | Set on `done` |
| `error` | TEXT, nullable | Set on `failed` or `cancelled` |
| `created_at` / `started_at` / `finished_at` | TEXT (ISO 8601 UTC), nullable | Timestamps for each transition |

## Condensed decision log

| Decision | Why (see TECHNICAL_OVERVIEW.md for full reasoning) |
|---|---|
| Docker Compose, 4 containers | Portable across OS, network-segmentable, atomic clean stop via `down` |
| Cloudflare Tunnel over port forwarding | Outbound-only — no inbound port on the router to scan/attack |
| Qwen2.5-7B-Instruct over Meditron/BioMistral | Actual use case needed long-context structured-data synthesis, not medical trivia Q&A; 32K vs 4K context was decisive |
| `OLLAMA_NUM_THREAD` pinned explicitly | Ollama's auto-detected thread count ignores Docker's cgroup CPU quota — mismatch caused a 38x slowdown |
| `OLLAMA_NUM_CTX` set explicitly | Ollama defaults to ~4096 tokens regardless of model capability — would have silently defeated the Qwen2.5 choice |
| Gateway timeout raised to 1200s | 300s was cutting real large-payload requests off mid-processing |
| `num_batch` tuning reverted | Measured negative result (slower, 2x RAM) — kept as a documented "tried, didn't help" |
| Async job pattern (`/v1/jobs`) | Cloudflare's 100s hard timeout makes synchronous large-payload calls structurally impossible over the public tunnel, regardless of local speed |
| SQLite job store, not in-memory | Survives `docker compose restart gateway`; an in-memory dict would silently lose queued/in-flight jobs |
| Single worker loop, not N concurrent | Ollama only processes one request at a time anyway; concurrent gateway-side requests would just queue invisibly inside Ollama instead of visibly inside ours |
| Per-model `daily_limit` via job history, not the generic rate limiter | `RATE_LIMIT_PER_MINUTE` is a short-window, model-agnostic limit; a per-model, 24-hour quota needed the job table's own history, which already had everything required |
| Data pre-filtering technique (demonstrated, not yet wired in) | 86.2% token reduction on the real test payload — the single largest lever found, bigger than any inference-engine tuning; deliberately left as application-layer logic, not built into the generic gateway |
| Requests address models by real Ollama tag directly, not a logical alias (e.g. `"insights"`) | Original design used aliases so a use case's default model could change without touching calling code; in practice the calling app always sends its own system message anyway (overriding the registry default), so the alias layer added indirection without adding real flexibility — simpler to have the gateway be a thin, honest passthrough: what you request is what runs. Trade-off: two different use cases sharing one physical model can no longer have separate `daily_limit` values from each other |
| Removed the default `system_prompt` fallback entirely | Once requests address real models directly, silently injecting text into a caller's messages contradicts "thin, honest passthrough" — the fallback was also never exercised in practice, since callers who care about behavior send their own `system` message. The gateway now never adds or rewrites anything; what you send is exactly what the model sees |
| `DELETE /v1/jobs/{id}` cancels via `asyncio.Task.cancel()`, not a cooperative flag | A flag the worker checks between steps can't interrupt an in-flight `await` on Ollama's response — the worker would just keep blocking on that one call regardless. Cancelling the task directly raises `CancelledError` at that exact await point, closing the connection immediately; confirmed live via Ollama's own `srv stop: cancel task` log line, not assumed |
| Warm-up targets only the first registry entry, not every model | `OLLAMA_MAX_LOADED_MODELS=1` means only one model is ever resident in RAM — warming a second model would immediately evict the first, wasting both warm-ups for whichever model is actually requested first in practice |
| Streaming (SSE) added to `/v1/chat` only, not `/v1/jobs` | The entire point of `/v1/jobs` is decoupling submission from execution so the client never holds a connection open — streaming requires exactly the opposite (an open connection for the stream's duration), so it doesn't fit that endpoint's purpose |
| Added Open WebUI (`webui`) talking directly to Ollama, bypassing the gateway | A private, local-only chat UI for interactive testing doesn't need API-key auth or rate limits — those exist for traffic arriving over the public internet, which this never does (bound to `127.0.0.1`, no tunnel route). Adding an auth layer here would be friction with no corresponding security benefit |
| Created a custom "Qwen2.5 7B (tuned)" model preset in Open WebUI with pinned `num_thread`/`num_ctx` | Open WebUI is a separate client hitting Ollama directly — it doesn't know the gateway pins these values, and a cold model load through the raw model entry silently reintroduces the exact thread-oversubscription and context-truncation bugs from Section 4 of `TECHNICAL_OVERVIEW.md`. Confirmed via a real cold-load test (`ollama stop` then a fresh request), not assumed |

## Known limitations / open items

- No admin UI or per-key usage dashboard yet — `job_store` already has everything needed (caller, model, timestamps) to build one.
- The data pre-filtering technique (Section 6.6 of the case study) is demonstrated on sample data but not yet wired into any real request path — it belongs in the calling application, not this gateway.
- LLM output accuracy on precise numeric direction-tracking (missed/reversed values in the health-data evaluation) is a known, measured weakness — recommended mitigation is deterministic pre-computation of deltas/flags in the calling application, not a gateway-side fix.
- All requests share Ollama's single processing slot regardless of model — a long-running job on one model makes every other call (even a quick request to a different model) wait until it finishes. Deferred by design choice, not an oversight; would need a second loaded model (`OLLAMA_MAX_LOADED_MODELS` ≥ 2) or a second Ollama instance to fix, at a real RAM/throughput cost.
