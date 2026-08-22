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
├── docker-compose.yml          # the 3-container stack: ollama, gateway, cloudflared
├── docker-compose.tunnel.yml   # override: switches cloudflared to a stable named tunnel
├── .env.example                # documented template — copy to .env, never commit .env
├── start.ps1 / stop.ps1        # lifecycle scripts (see docs/OPERATIONS.md)
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
        ├── ollama_client.py     # talks to Ollama's HTTP API; registry loading; system-prompt injection
        ├── job_store.py         # SQLite-backed async job queue (the persistence layer)
        ├── job_worker.py        # single-consumer background loop that actually runs jobs
        ├── check_pending_jobs.py  # standalone script stop.ps1 uses to warn about unfinished jobs
        ├── models_registry.yaml  # THE plug-in point: logical name -> ollama tag + prompt + limits
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
Three things live here:
- `load_registry()` / `resolve_model_entry()` — reads `models_registry.yaml`
  fresh on every call (no caching), so registry edits take effect without a
  gateway restart.
- `with_system_prompt()` — shared logic for injecting a model's default
  `system_prompt` unless the caller already supplied one. Used by both
  `routes/chat.py` (synchronous path) and `job_worker.py` (async path) so
  the two can never drift apart in behavior.
- `chat()` — the actual POST to `/api/chat`, passing `num_thread`/`num_ctx`
  explicitly (see TECHNICAL_OVERVIEW.md §4 for why Ollama's own defaults
  aren't trustworthy here) with a 1200-second timeout sized for large
  real-world payloads.

**`job_store.py`** — all persistence for the async job system, backed by a
single SQLite table (`jobs`) on the `gateway_data` volume. Every function
comes in a `_sync` (plain `sqlite3`, blocking) and an async wrapper
(`asyncio.to_thread(...)`) so the blocking C extension never stalls the
event loop that's also serving HTTP requests. Key functions: `create_job`,
`claim_next_job` (atomically picks the oldest queued job and marks it
processing — only ever called from the one worker loop, so no race to guard
against), `finish_job`, `queue_position`, `count_recent_jobs` (backs
per-model daily limits), `set_ollama_tag` (records the real Ollama tag a job
actually ran under, e.g. `qwen2.5:7b-instruct-q4_K_M`, distinct from the
logical name the caller requested — set at processing time so it reflects
what genuinely ran even if the registry changes later), and
`mark_stale_processing_failed` (run once at startup — see "Job lifecycle"
below).

**`job_worker.py`** — `run_worker_loop()`, an `asyncio` task started from
`main.py`'s lifespan hook. A simple claim-process-repeat loop, deliberately
single-consumer: Ollama itself only processes one request at a time
(`OLLAMA_NUM_PARALLEL=1`), so running more than one job concurrently from the
gateway side would just mean multiple of our own requests piling up inside
Ollama's own opaque internal queue instead of ours, breaking the accuracy of
`queue_position`.

**`routes/chat.py`** — `POST /v1/chat`. Validates the model exists, injects
the system prompt via `with_system_prompt`, calls `ollama_client.chat()`
directly and waits for the result. Rate-limited via the `slowapi` decorator.

**`routes/jobs.py`** — `POST /v1/jobs` (validates the model, enforces the
model's `daily_limit` if configured via `job_store.count_recent_jobs`, then
just inserts a row and returns `202` — the actual work happens later, in the
worker loop) and `GET /v1/jobs/{id}` (looks up the row, checks the caller
owns it, shapes the response based on `status`).

**`main.py`** — assembles the FastAPI app: CORS, the rate-limit exception
handler, all three routers, and the `/healthz` endpoint. The `lifespan`
context manager is where async-job infrastructure actually starts up: DB
init, failing any orphaned `processing` jobs, then spawning the worker task.

**`models_registry.yaml`** — not code, but functionally the most important
file for extending this project. Every logical model name maps to an Ollama
tag plus two optional fields: `system_prompt` (default behavior, overridable
per-request) and `daily_limit` (per-key, rolling-24h cap, enforced only on
`/v1/jobs`). Adding a model is edit-this-file-and-pull, no Python changes.

## Workflows

### Synchronous request (`/v1/chat`)

```
client → gateway: POST /v1/chat {model, messages}
  gateway: require_api_key (401 if invalid)
  gateway: rate limit check (429 if exceeded)
  gateway: resolve_model_entry(model) (404 if unknown)
  gateway: with_system_prompt(entry, messages)
  gateway → ollama: POST /api/chat {tag, messages, options}
    ollama: load model if not already warm, run inference
  ollama → gateway: {message: {role, content}}
gateway → client: 200 {model, message}
```
One HTTP connection held open for the entire duration — fine for `chat`
(seconds), unworkable for `insights` on real data (minutes) once anything
sits between the client and gateway with its own timeout (browsers, mobile
clients, and specifically Cloudflare's free-tier 100-second proxy limit).

### Asynchronous request (`/v1/jobs`)

```
client → gateway: POST /v1/jobs {model, messages}
  gateway: require_api_key, resolve_model_entry, daily_limit check
  gateway → job_store: create_job(...)  [status=queued]
gateway → client: 202 {job_id, status: "queued"}          <-- returns immediately

  [independently, in the background:]
  job_worker loop: claim_next_job()      [status=queued -> processing]
  job_worker: resolve_model_entry -> job_store.set_ollama_tag(...)
  job_worker: with_system_prompt + ollama_client.chat(...)
  job_worker → job_store: finish_job(...) [status=processing -> done/failed]

client → gateway: GET /v1/jobs/{id}   (polled repeatedly)
gateway → job_store: get_job(id), queue_position(id) if still queued
gateway → client: 200 {status, queue_position | result | error}
```
The submit call and the actual model call are fully decoupled — the client
never holds a long connection open, and a burst of submissions queues
visibly (`queue_position`) instead of silently piling up or timing out.

### Startup sequence (`main.py` lifespan)

```
1. job_store.init_db()                       — create the jobs table if absent, run column migrations (e.g. ollama_tag) if it already exists
2. job_store.mark_stale_processing_failed()  — fail anything orphaned by a prior crash/restart
3. asyncio.create_task(run_worker_loop())    — start claiming queued jobs
4. (app now serving requests)
...
5. (on shutdown) worker_task.cancel()
```

## Data model (`jobs` table)

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT (UUID) | Primary key, returned to the client as `job_id` |
| `caller` | TEXT | App name from the API key, used for ownership checks and daily-limit counting |
| `model` | TEXT | Logical model name requested (e.g. `"insights"`) |
| `messages` | TEXT (JSON) | The original request body's messages, as submitted |
| `status` | TEXT | `queued` \| `processing` \| `done` \| `failed` |
| `ollama_tag` | TEXT, nullable | The real underlying model that ran this job (e.g. `"qwen2.5:7b-instruct-q4_K_M"`), set by the worker at processing time — `null` while still `queued`, and stays `null` if the job failed before a model was ever resolved (e.g. an unknown-model error) |
| `result` | TEXT (JSON), nullable | Set on `done` |
| `error` | TEXT, nullable | Set on `failed` |
| `created_at` / `started_at` / `finished_at` | TEXT (ISO 8601 UTC), nullable | Timestamps for each transition |

## Condensed decision log

| Decision | Why (see TECHNICAL_OVERVIEW.md for full reasoning) |
|---|---|
| Docker Compose, 3 containers | Portable across OS, network-segmentable, atomic clean stop via `down` |
| Cloudflare Tunnel over port forwarding | Outbound-only — no inbound port on the router to scan/attack |
| Qwen2.5-7B-Instruct over Meditron/BioMistral | Actual use case needed long-context structured-data synthesis, not medical trivia Q&A; 32K vs 4K context was decisive |
| `OLLAMA_NUM_THREAD` pinned explicitly | Ollama's auto-detected thread count ignores Docker's cgroup CPU quota — mismatch caused a 38x slowdown |
| `OLLAMA_NUM_CTX` set explicitly | Ollama defaults to ~4096 tokens regardless of model capability — would have silently defeated the Qwen2.5 choice |
| Gateway timeout raised to 1200s | 300s was cutting real large-payload requests off mid-processing |
| `num_batch` tuning reverted | Measured negative result (slower, 2x RAM) — kept as a documented "tried, didn't help" |
| Async job pattern (`/v1/jobs`) | Cloudflare's 100s hard timeout makes synchronous `insights` calls structurally impossible over the public tunnel, regardless of local speed |
| SQLite job store, not in-memory | Survives `docker compose restart gateway`; an in-memory dict would silently lose queued/in-flight jobs |
| Single worker loop, not N concurrent | Ollama only processes one request at a time anyway; concurrent gateway-side requests would just queue invisibly inside Ollama instead of visibly inside ours |
| Per-model `daily_limit` via job history, not the generic rate limiter | `RATE_LIMIT_PER_MINUTE` is a short-window, model-agnostic limit; a per-model, 24-hour quota needed the job table's own history, which already had everything required |
| Data pre-filtering technique (demonstrated, not yet wired in) | 86.2% token reduction on the real test payload — the single largest lever found, bigger than any inference-engine tuning; deliberately left as application-layer logic, not built into the generic gateway |

## Known limitations / open items

- `stream: true` is accepted by the request schema but not implemented (returns `400`).
- No admin UI or per-key usage dashboard yet — `job_store` already has everything needed (caller, model, timestamps) to build one.
- The data pre-filtering technique (Section 6.6 of the case study) is demonstrated on sample data but not yet wired into any real request path — it belongs in the calling application, not this gateway.
- LLM output accuracy on precise numeric direction-tracking (missed/reversed values in the health-data evaluation) is a known, measured weakness — recommended mitigation is deterministic pre-computation of deltas/flags in the calling application, not a gateway-side fix.
- `chat` and `insights` currently share Ollama's single processing slot — a long `insights` job makes `chat` unresponsive to everyone until it finishes. Deferred by design choice, not an oversight; would need a second loaded model (`OLLAMA_MAX_LOADED_MODELS` ≥ 2) or a second Ollama instance to fix, at a real RAM/throughput cost.
