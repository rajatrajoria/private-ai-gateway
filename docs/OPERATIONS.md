# Operations Guide

Everything needed to run, manage, and troubleshoot this stack day to day.
For *why* it's built this way, see [`TECHNICAL_OVERVIEW.md`](../TECHNICAL_OVERVIEW.md).
For the API itself, see [`docs/API.md`](API.md).

## Prerequisites

- **Docker Desktop**, with the WSL2 backend (Windows). Requires hardware
  virtualization enabled — if Docker Desktop shows "Virtualization support
  not detected," see Troubleshooting below.
- A terminal (PowerShell on Windows, any shell elsewhere).

## Starting and stopping

```bash
./start.ps1
```
What it does, in order:
1. Creates `.env` from `.env.example` if missing (first run only).
2. Checks whether `TUNNEL_TOKEN` is set in `.env`, and picks the matching
   Docker Compose file set: just `docker-compose.yml` (free Quick Tunnel) if
   blank, or that plus `docker-compose.tunnel.yml` (your stable named tunnel)
   if set.
3. Runs `docker compose up -d --build` — builds the gateway image if its
   source changed, starts all three containers in the background.
4. Polls `http://127.0.0.1:8000/healthz` every 2 seconds (up to 60s) until
   the gateway responds, so you get a clear "up" or "not up" signal.
5. If using the Quick Tunnel, greps `docker compose logs cloudflared` for the
   `trycloudflare.com` URL and prints it.
6. Prints `docker compose ps` so you can see final container status.

```bash
./stop.ps1
```
What it does:
1. Checks for any job still `queued` or `processing` (via a one-off script,
   `gateway/app/check_pending_jobs.py`, run inside the gateway container) and
   warns you — a `processing` job will be marked `failed` since it can't
   resume; a `queued` one is safe and will simply run after the next start.
2. Runs `docker compose down` — stops and **removes** all three containers
   and the network. This is not `docker compose stop`: `stop` merely pauses
   containers and leaves them holding their resources; `down` fully removes
   them, which is what actually frees RAM/CPU and drops the Cloudflare
   connection. Named volumes (model weights, job database) are *not*
   removed — only ephemeral container state is.
3. Prints `docker compose ps` to confirm nothing is left running.

## Everyday Docker Compose commands

Run these from the `private-ai-gateway/` directory.

| Command | What it does |
|---|---|
| `docker compose ps` | Lists this project's containers and their status |
| `docker compose logs -f` | Streams logs from all three containers, live |
| `docker compose logs -f gateway` | Streams just the gateway's logs (Uvicorn access log — one line per request) |
| `docker compose logs -f ollama` | Streams just Ollama's logs — model loading, per-token timing, errors |
| `docker compose logs --tail 50 <service>` | Last 50 lines only, no follow |
| `docker compose exec ollama ollama list` | Lists model weights currently pulled |
| `docker compose exec ollama ollama pull <tag>` | Downloads a model — see "Adding a model" below |
| `docker compose exec ollama ollama rm <tag>` | Deletes a downloaded model's weights, freeing disk space |
| `docker compose restart gateway` | Restarts just the gateway (e.g. after editing `models_registry.yaml`'s Python-adjacent behavior — note the registry YAML itself is read fresh on every request, no restart needed for registry-only edits) |
| `docker compose up -d --build gateway` | Rebuilds the gateway image (needed after any Python code change) and restarts it |
| `docker compose exec gateway python -m app.check_pending_jobs` | Manually check for unfinished jobs (what `stop.ps1` runs automatically) |
| `docker compose config` | Validates the compose file(s) without starting anything — good after editing YAML |
| `docker compose down -v` | **Destructive** — also deletes named volumes (re-downloads all models, wipes job history). Not used by `stop.ps1` on purpose. Only run this deliberately. |

## Adding or changing a model

1. Find the tag on [ollama.com/library](https://ollama.com/library) — **verify it exists before pulling**; a wrong or community-namespace tag (e.g. a model only available as `someuser/modelname`, not in the official library) will fail with `pull model manifest: file does not exist`.
2. Edit [`gateway/app/models_registry.yaml`](../gateway/app/models_registry.yaml):
   ```yaml
   your_model_name:
     ollama_tag: <the tag>
     description: "What this is for"
     system_prompt: >
       Optional default behavior instructions.
     daily_limit: 200   # optional per-key daily cap, checked on /v1/jobs only
   ```
3. `docker compose exec ollama ollama pull <the tag>`
4. No gateway restart needed — the registry file is read fresh on every request.

## Managing API keys

- **Format**: `API_KEYS=name:key,name:key,...` in `.env`, one entry per calling application.
- **Generate a new key**: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- **Rotate a key**: edit `.env` with the new value, then `docker compose up -d gateway` (env vars are read at container start, not hot-reloaded — a plain `restart` isn't enough if `.env` changed, use `up -d` to force recreation). The old key stops working immediately.
- **Revoke a key**: remove its `name:key` entry from `API_KEYS` and `docker compose up -d gateway`.
- Treat any key that's been pasted into chat logs, screenshots, or shared documents as compromised by exposure — rotate it, even if you don't think anyone else actually saw it.

## Cloudflare Tunnel modes

| | No `TUNNEL_TOKEN` | `TUNNEL_TOKEN` set |
|---|---|---|
| Compose files used | `docker-compose.yml` only | `docker-compose.yml` + `docker-compose.tunnel.yml` |
| cloudflared command | `tunnel --url http://gateway:8000` (Quick Tunnel) | `tunnel run` (named tunnel) |
| URL | Random `*.trycloudflare.com`, changes every restart | Stable `api.yourdomain.com` |
| Setup needed | None | Cloudflare account + domain — see [`cloudflared/README.md`](../cloudflared/README.md) |

Find the current Quick Tunnel URL with:
```bash
docker compose logs cloudflared | grep trycloudflare
```

## Monitoring resource usage

```bash
docker stats
```
Live view of CPU/RAM per container — useful for confirming the `mem_limit`/`cpus` ceilings in `docker-compose.yml` are being respected and for watching a request actually consume resources in real time.

## Full command reference used during development

For deep debugging, these are the exact diagnostic commands used while building this project (see `TECHNICAL_OVERVIEW.md` for the incidents they were used to solve):

```bash
# CPU topology (Windows, PowerShell)
Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors

# Validate compose files without starting anything
docker compose config --quiet

# Check a specific job's row directly (bypassing the API, for debugging only)
docker compose exec gateway python -c "import sqlite3; c=sqlite3.connect('/data/jobs.db'); print(c.execute('SELECT * FROM jobs').fetchall())"
```

---

## Troubleshooting

### "Virtualization support not detected" (Docker Desktop won't start)
Two possible causes, try in order:
1. Run `wsl --install` in an **elevated** PowerShell/Terminal, then restart the machine.
2. If that doesn't fix it, virtualization (Intel VT-x/AMD-V) may be disabled in BIOS/UEFI — reboot into firmware setup (commonly F2/Del on boot), enable "Intel Virtualization Technology" or equivalent, save and exit.

### `ollama pull` fails with "pull model manifest: file does not exist"
The tag doesn't exist in Ollama's official library — either a typo, or the model is only available under a community user namespace (e.g. `someuser/modelname`). Check [ollama.com/library](https://ollama.com/library) directly before assuming a tag is correct.

### A request seems to hang forever / extremely slow (well under 1 token/sec)
Almost certainly a mismatch between `OLLAMA_NUM_THREAD` (in `.env`) and `cpus:` (in `docker-compose.yml`, `ollama` service) — they must be set to the **same** number. Ollama auto-detects the host's full core count for its thread pool by default, which oversubscribes against a smaller cgroup CPU quota and causes severe contention. Check `docker compose logs ollama` for a line like `llama threadpool init, n_threads = N` and compare it to your `cpus:` value.

### Request fails with `{"detail": "Ollama backend error: "}` (empty detail)
This is a client-side timeout (`_CHAT_TIMEOUT_SECONDS` in `gateway/app/ollama_client.py`, currently 1200s) firing before Ollama finished — httpx timeout exceptions often stringify to nothing. Check `docker compose logs ollama` for the exact point it was cancelled (`srv stop: cancel task`). If it's a genuinely large payload needing more time, either raise the timeout further or — better — use `/v1/jobs` instead of `/v1/chat` so there's no client-side timeout to hit at all.

### `insights`-class requests fail over the public URL but work locally
Cloudflare's free tier enforces a hard, non-configurable 100-second timeout on every proxied request — no local setting changes this. Use `POST /v1/jobs` + polling for anything that might take longer than that; `/v1/chat` will never work for a multi-minute request over the tunnel, regardless of local timeout settings.

### PowerShell `curl` gives a "Cannot bind parameter" or garbled JSON error
PowerShell's built-in `curl` is an alias for `Invoke-WebRequest`, not real curl — it doesn't understand `-H`/`-d` syntax. Either call `curl.exe` explicitly (bypasses the alias) and wrap JSON bodies in **single** quotes (PowerShell double-quoted strings don't support backslash-escaping the way Bash does), or use `Invoke-RestMethod` with a `-Headers` hashtable and a single-quoted `-Body` string instead.

### Permission denied writing to a new volume mount (non-root container)
The gateway runs as a non-root user (`appuser`, uid 10001) with a read-only root filesystem. Any new writable path needs to be pre-created and `chown`'d to that user *in the Dockerfile*, before `USER appuser` — a fresh named volume mounted onto a path that already exists in the image inherits that path's ownership on first creation. See how `/data` is handled in `gateway/Dockerfile` as the reference pattern for adding another writable path.

### A job stays `queued` forever / queue seems stuck
Check the worker loop is actually alive: `docker compose logs gateway` should show it periodically claiming jobs (or check `docker compose ps` — if the `gateway` container isn't `Up`, nothing is processing). Also check `GET /v1/jobs/{id}` for `queue_position` — a nonzero position means other jobs are legitimately ahead of it; jobs are processed strictly one at a time.

### A job is stuck `processing` and never finishes
If the gateway container was restarted, any job that was `processing` at that moment is automatically marked `failed` on the next startup (`job_store.mark_stale_processing_failed`, run from `main.py`'s lifespan hook) — it can't resume, so poll again after a restart rather than waiting indefinitely.

### `docker compose up` with the tunnel override fails or does nothing different
Confirm `TUNNEL_TOKEN` is actually non-empty in `.env` — `start.ps1` decides which compose files to load based on a regex match against that value, not just the file existing.

### Need to fully reset everything, including downloaded models and job history
```bash
docker compose down -v
```
This is destructive and not run by any of the provided scripts on purpose — it deletes the named volumes (`ollama_data`, `gateway_data`), meaning every model gets re-downloaded and all job history is lost. Only run it deliberately.
