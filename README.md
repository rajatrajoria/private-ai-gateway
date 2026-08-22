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
  rate-limits, maps a logical model name (`"insights"`, `"chat"`, ...) to the
  real underlying model, and can attach a default system prompt per model
- **`cloudflared`** — an outbound-only tunnel that gives your gateway a public
  HTTPS URL without opening any port on your router

All three run together via Docker Compose and stop/start as one unit.

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (with WSL2 backend on Windows)
- A free [Cloudflare](https://dash.cloudflare.com/sign-up) account + a domain, if you want public access (optional — you can run purely locally without this)

## Quickstart

```bash
git clone <this-repo>
cd private-ai-gateway
./start.ps1
```

`start.ps1` copies `.env.example` to `.env` on first run — **edit `.env` before
exposing this publicly** and set a real `API_KEYS` value.

Pull the primary model once the stack is up (only needs doing once — it's
cached in a Docker volume after that). This one backs both `insights` and
`chat`:

```bash
docker compose exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M
```

Optionally, also pull the narrower domain-tuned model used by `medical_qa`
(see `gateway/app/models_registry.yaml` for why it's kept separate):

```bash
docker compose exec ollama ollama pull meditron:7b-q4_K_M
```

Test it locally:

```bash
curl http://127.0.0.1:8000/healthz

curl -H "Authorization: Bearer <your key from .env>" \
     http://127.0.0.1:8000/v1/models

curl -H "Authorization: Bearer <your key from .env>" \
     -H "Content-Type: application/json" \
     -d '{"model":"insights","messages":[{"role":"user","content":"Here is a week of my lab results and daily metrics as JSON: {...}. What stands out?"}]}' \
     http://127.0.0.1:8000/v1/chat
```

To make it reachable by your other hosted apps over the internet, follow
[`cloudflared/README.md`](./cloudflared/README.md) (one-time setup).

> Until you do that, `docker compose ps` will show `cloudflared` as `Exited` —
> that's expected (it has no tunnel token yet to connect with). `ollama` and
> `gateway` are unaffected and work fine for local testing in the meantime.

## Stopping it

```bash
./stop.ps1
```

This removes every container and network for this stack — no lingering
processes, no RAM/CPU held, and the public URL stops resolving to this machine.
Model files stay cached on disk so the next start is fast. See
`TECHNICAL_OVERVIEW.md` for exactly what "clean stop" means here.

## Adding or swapping a model

Edit [`gateway/app/models_registry.yaml`](./gateway/app/models_registry.yaml):

```yaml
your_model_name:
  ollama_tag: <any tag from https://ollama.com/library>
  description: "What this model is for"
  system_prompt: >
    Optional. Applied automatically unless the caller's request already
    includes its own system message — lets you bake in "how should this
    model behave" per logical name without changing app code.
```

Then:

```bash
docker compose exec ollama ollama pull <the tag>
docker compose restart gateway
```

No Python code changes required — this is the entire "plug in / plug out" mechanism.

## API reference

| Endpoint | Auth | Description |
|---|---|---|
| `GET /healthz` | none | Liveness check, also reports whether Ollama is reachable |
| `GET /v1/models` | Bearer key | Lists logical model names available |
| `POST /v1/chat` | Bearer key | Synchronous — waits for the full response. Fine for `chat` (fast); **do not use for `insights`** (see below) |
| `POST /v1/jobs` | Bearer key | Asynchronous — same body as `/v1/chat`, returns `202` + a job ID immediately |
| `GET /v1/jobs/{job_id}` | Bearer key | Poll for a submitted job's status/result |

### Why two ways to call the model

`insights`-class requests on real data take several minutes on CPU-only hardware — too long for most HTTP clients, and hard-killed after 100 seconds by Cloudflare's free-tier tunnel regardless of local settings (see `TECHNICAL_OVERVIEW.md`). Use `/v1/jobs` for anything that might run long:

```bash
# 1. Submit — returns immediately
curl -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
     -d '{"model":"insights","messages":[{"role":"user","content":"..."}]}' \
     http://127.0.0.1:8000/v1/jobs
# -> {"job_id": "…", "status": "queued"}

# 2. Poll until status is "done" or "failed"
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8000/v1/jobs/<job_id>
# -> {"status": "queued", "queue_position": 0, ...}
# -> {"status": "processing", ...}
# -> {"status": "done", "result": {"message": {...}}, ...}
```

Jobs are processed **one at a time**, matching Ollama's own `OLLAMA_NUM_PARALLEL=1` — a burst of submissions queues up rather than failing, and `queue_position` tells the caller how many jobs are ahead of theirs. Job state is stored in a small SQLite database on a persistent volume, so it survives a `docker compose restart gateway`; a job that was actively `processing` when the container restarted is marked `failed` (it can't resume), while anything still `queued` is untouched and simply gets picked up again.

`chat` requests are short enough that `/v1/chat` is still the simpler choice for them.

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
