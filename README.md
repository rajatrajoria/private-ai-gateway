# Private AI Gateway

Turn any spare laptop into your own private AI API — pluggable local models
(health-data synthesis, general chat, whatever you add), served to your other
apps over an authenticated HTTPS endpoint, with no ports forwarded on your router.

Read [`TECHNICAL_OVERVIEW.md`](./TECHNICAL_OVERVIEW.md) for the full architecture
and the reasoning behind every design decision — start there if you want to
understand *why* this is built this way, not just *how* to run it.

**Documentation map:**
- [`docs/API.md`](./docs/API.md) — full API reference (endpoints, auth, request/response shapes, error codes) for anyone integrating with this gateway
- [`docs/OPERATIONS.md`](./docs/OPERATIONS.md) — every command explained, plus a troubleshooting playbook built from real incidents hit while developing this
- [`docs/PROJECT_STRUCTURE.md`](./docs/PROJECT_STRUCTURE.md) — file-by-file code map, request/job workflows, and a condensed decision log for anyone extending this codebase

## What this is

- **`ollama`** — runs the actual AI models locally (CPU-friendly, quantized)
- **`gateway`** — a small FastAPI service in front of it: checks API keys,
  rate-limits, and validates the requested model against an allowlist
  (`gateway/app/models_registry.yaml`). Requests name the real Ollama model
  directly (e.g. `"qwen2.5:7b-instruct-q4_K_M"`) — there's no separate alias
  layer, and the gateway never adds or rewrites anything in your messages.
  Also warms up the first registry model at startup so the first real
  request doesn't pay Ollama's cold-load penalty
- **`cloudflared`** — an outbound-only tunnel that gives your gateway a public
  HTTPS URL without opening any port on your router
- **`webui`** ([Open WebUI](https://github.com/open-webui/open-webui)) — a
  private, local-only chat interface for talking to your models directly
  (prompt testing, comparing models, whatever you want to poke at). Bound to
  `127.0.0.1:3000` and never routed through the tunnel, so it's never reachable
  from the internet. Talks straight to Ollama, bypassing the gateway's
  API-key auth entirely — that auth exists for traffic arriving over the
  public internet, which this never does

All four run together via Docker Compose and stop/start as one unit.

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (with WSL2 backend on Windows)
- A free [Cloudflare](https://dash.cloudflare.com/sign-up) account + a domain, if you want public access (optional — you can run purely locally without this)

## Quickstart

```bash
git clone <this-repo>
cd private-ai-gateway

# Windows (PowerShell):
./start.ps1

# Mac/Linux:
chmod +x start.sh stop.sh   # only needed once — git doesn't always preserve
                             # the executable bit across platforms
./start.sh
```

Both scripts do the same thing: check Docker is installed and running
(starting it for you if it isn't — Docker Desktop on Mac/Windows, or the
`docker` systemd service on Linux — and telling you exactly what to do if it
truly can't), copy `.env.example` to `.env` on first run, then build and
start the stack. **Edit `.env` before exposing this publicly** and set a
real `API_KEYS` value.

Pull the primary model once the stack is up (only needs doing once — it's
cached in a Docker volume after that):

```bash
docker compose exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M
```

Optionally, also pull the narrower domain-tuned model (see
`gateway/app/models_registry.yaml` for why it's kept separate):

```bash
docker compose exec ollama ollama pull meditron:7b-q4_K_M
```

Test it locally — note the request's `model` field is the exact tag you just pulled:

```bash
curl http://127.0.0.1:8000/healthz

curl -H "Authorization: Bearer <your key from .env>" \
     http://127.0.0.1:8000/v1/models

curl -H "Authorization: Bearer <your key from .env>" \
     -H "Content-Type: application/json" \
     -d '{"model":"qwen2.5:7b-instruct-q4_K_M","messages":[{"role":"user","content":"What'"'"'s a healthy resting heart rate range?"}]}' \
     http://127.0.0.1:8000/v1/chat
```

That used `/v1/chat` because it's a short question. For anything that might
take more than a minute or so on real data, use `/v1/jobs` instead — see
"Why two ways to call the model" below.

You also have a private chat UI running at
[http://127.0.0.1:3000](http://127.0.0.1:3000) — only reachable from this
machine. **Use the "Qwen2.5 7B (tuned)" model in its selector, not the raw
`qwen2.5:7b-instruct-q4_K_M` entry** — the raw entry has no thread/context
settings attached, so on a cold model load it falls back to Ollama's own
defaults (auto-detected thread count, ~4K context), silently reintroducing
the exact performance and context-truncation bugs documented in
`TECHNICAL_OVERVIEW.md` §4. The "(tuned)" preset pins the same
`num_thread`/`num_ctx` values the gateway itself uses, and is already set as
the default model so a fresh browser session picks it automatically.

You now already have a public URL, without any extra setup — `start.ps1`/
`start.sh` starts a free Cloudflare Quick Tunnel automatically when
`TUNNEL_TOKEN` is blank in `.env`, and prints it. Find it anytime with:

```bash
docker compose logs cloudflared | grep trycloudflare
```

That URL changes every restart, so it's for testing, not a permanent
integration. When you're ready for a stable `api.yourdomain.com`, follow
[`cloudflared/README.md`](./cloudflared/README.md) (one-time setup) — nothing
else changes, `start.ps1`/`start.sh` detects `TUNNEL_TOKEN` and switches over automatically.

## Stopping it

```bash
./stop.ps1   # Windows
./stop.sh    # Mac/Linux
```

This removes every container and network for this stack — no lingering
processes, no RAM/CPU held, and the public URL stops resolving to this machine.
Model files stay cached on disk so the next start is fast. See
`TECHNICAL_OVERVIEW.md` for exactly what "clean stop" means here.

## Adding or swapping a model

Edit [`gateway/app/models_registry.yaml`](./gateway/app/models_registry.yaml) —
keyed directly by the real Ollama tag, since that's exactly what callers put
in the request's `model` field:

```yaml
qwen2.5:14b-instruct-q4_K_M:   # any tag from https://ollama.com/library
  description: "What this model is for"
  daily_limit: 200   # optional: per-API-key cap on /v1/jobs submissions per rolling 24h
```

The gateway is a pure passthrough — it never adds or rewrites anything in the
messages you send. If you want specific default behavior, send your own
`system` message with each request.

Then:

```bash
docker compose exec ollama ollama pull <the tag>
docker compose restart gateway
```

No Python code changes required. The registry is an allowlist plus optional
per-model config — a model must be listed here to be callable at all (a typo
or an unpulled model fails with a clear `404` instead of a confusing later
error), but the request itself always names the real model directly.

## API reference

| Endpoint | Auth | Description |
|---|---|---|
| `GET /healthz` | none | Liveness check, also reports whether Ollama is reachable |
| `GET /v1/models` | Bearer key | Lists the real Ollama model names you can call |
| `POST /v1/chat` | Bearer key | Synchronous — waits for the full response, or streams tokens via SSE with `stream: true`. Fine for short requests; **avoid for anything that might run long** (see below) |
| `POST /v1/jobs` | Bearer key | Asynchronous — same body as `/v1/chat`, returns `202` + a job ID immediately |
| `GET /v1/jobs/{job_id}` | Bearer key | Poll for a submitted job's status/result |
| `DELETE /v1/jobs/{job_id}` | Bearer key | Cancel a job — a `queued` one is simply dropped, a `processing` one is actually interrupted (Ollama itself stops computing, not just marked cancelled locally) so the next queued job starts immediately |

### Why two ways to call the model

A large payload on real data can take several minutes of CPU-only inference —
too long for most HTTP clients, and hard-killed after 100 seconds by
Cloudflare's free-tier tunnel regardless of local settings (see
`TECHNICAL_OVERVIEW.md`). Use `/v1/jobs` for anything that might run long:

```bash
# 1. Submit — returns immediately
curl -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
     -d '{"model":"qwen2.5:7b-instruct-q4_K_M","messages":[{"role":"user","content":"..."}]}' \
     http://127.0.0.1:8000/v1/jobs
# -> {"job_id": "…", "status": "queued"}

# 2. Poll until status is "done" or "failed"
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8000/v1/jobs/<job_id>
# -> {"model": null, "status": "queued", "queue_position": 0, ...}
# -> {"model": "qwen2.5:7b-instruct-q4_K_M", "status": "processing", ...}
# -> {"model": "qwen2.5:7b-instruct-q4_K_M", "status": "done", "result": {"message": {...}}, ...}
```

`model` in the response is `null` only while a job is still `queued` — nothing
has actually started running yet to name; it's set the instant processing begins.

Jobs are processed **one at a time**, matching Ollama's own `OLLAMA_NUM_PARALLEL=1` — a burst of submissions queues up rather than failing, and `queue_position` tells the caller how many jobs are ahead of theirs. Job state is stored in a small SQLite database on a persistent volume, so it survives a `docker compose restart gateway`; a job that was actively `processing` when the container restarted is marked `failed` (it can't resume), while anything still `queued` is untouched and simply gets picked up again.

For short requests, `/v1/chat` is still the simpler choice — it's just a normal synchronous call.

## Disclaimer

This is a personal-scale project. If you're feeding it your own lab results,
daily metrics, or medications — as intended — treat the model's output as a
starting point for noticing patterns, not a diagnosis or treatment plan.
Nothing here is a substitute for a clinician. If you extend this to store or
log request bodies (the gateway does not by default), remember those bodies
may contain your own sensitive health data — keep any such logs local and
out of version control.

## License

MIT — see [`LICENSE`](./LICENSE).
