# Technical Overview — how this works, and why

This document exists so you can understand and extend this system yourself, not
just run it. Every design decision below has a reason; if you disagree with one
once you understand it, you now have enough context to change it correctly.

## 1. The big picture

```
Internet ──HTTPS──▶ Cloudflare Edge ──outbound tunnel──▶ [cloudflared container]
                                                                  │
                                                     (Docker "default" network)
                                                                  ▼
                                                    [gateway container: FastAPI]
                                                    - checks API key (Bearer token)
                                                    - rate-limits per key
                                                    - looks up "insights" in the
                                                      model registry → real tag
                                                                  │
                                                     (Docker "backend" network)
                                                                  ▼
                                                    [ollama container: model runtime]
                                                    (quantized 7B model, CPU inference)
```

Three containers, one `docker-compose.yml`, two Docker networks. Nothing runs
outside Docker except Docker Desktop itself.

## 2. Why each piece was chosen

**Ollama**, not raw `llama.cpp` or a Python inference script: it manages model
downloading, quantization format selection, and — critically — automatic RAM
unloading of idle models, for free. You interact with it over a simple local
HTTP API (`/api/chat`), so swapping which weights actually run is a config
change, not a code change.

**FastAPI**, not Flask/raw sockets: async by default (matters because a chat
request might sit waiting on the model for a long time — you don't want that
to block other requests), automatic request validation via Pydantic (a
malformed request body gets rejected before your code even runs), and it's the
natural choice given you already chose Python.

**Docker Compose**, not native processes: one `docker compose up` / `down`
command boots or completely tears down the whole stack identically on any OS.
This is *the* reason your "clean stop with zero leftover processes" requirement
is easy to guarantee — `docker compose down` doesn't just stop containers, it
removes them and the network they were on. There is nothing left to "leak."

**Cloudflare Tunnel**, not port-forwarding + dynamic DNS: explained in depth in
section 5, but in short — it means your router never accepts an inbound
connection from the internet at all.

## 3. Docker networking — why `ollama` can't be reached from outside

Docker containers on the same network can reach each other by service name
(Docker runs a tiny internal DNS server) — that's how `gateway` calls
`http://ollama:11434` without knowing any IP address. Containers are *not*
reachable from the host machine or the internet unless you explicitly publish
a port with `ports: ["host:container"]` in `docker-compose.yml`.

Look at `docker-compose.yml`:
- `ollama` has **no `ports:` entry at all** — there is no way to reach it except
  from another container on its network. It's on the `backend` network, which
  only `gateway` also joins. `cloudflared` was deliberately *not* added to
  `backend` — even if `cloudflared` were somehow compromised, it has no network
  route to `ollama` at all, only to `gateway`.
- `gateway` publishes `127.0.0.1:8000:8000` — bound to the loopback interface
  specifically (not `0.0.0.0`), so only processes on this exact machine can hit
  it directly (useful for your own `curl` testing). Nothing on your LAN, and
  nothing on the internet, can reach that port — the only way in from outside
  is through `cloudflared`, which talks to `gateway` over the Docker network,
  not through that published port at all.

This is why the model runtime is "isolated": it was never given a path to be
reached from anywhere except the one component that authenticates requests
first.

## 4. Resource limits — why the machine won't slow down

Docker enforces CPU/memory limits using **Linux cgroups** (control groups) — a
kernel feature that lets you say "this process tree may use at most N bytes of
RAM and M CPU cores," enforced by the kernel itself, not by the application.
On Windows, Docker Desktop runs its containers inside a lightweight WSL2 Linux
VM, so the same cgroup enforcement applies there too.

In `docker-compose.yml`:
```yaml
ollama:
  mem_limit: 10g        # hard ceiling
  mem_reservation: 4g   # soft target under contention
  cpus: "5"              # out of however many logical cores you have
```
If Ollama's process tries to allocate more than `mem_limit`, the kernel's
out-of-memory killer terminates *the process inside the container* — Windows
itself never feels memory pressure from this. The request that triggered it
just fails (the gateway returns a 500), which is a far better outcome than your
whole laptop swapping and becoming unresponsive.

`cpus: "5"` limits Ollama to 5 logical cores' worth of scheduling time, leaving
the rest free for Windows and whatever else you're doing while the server runs.

Three Ollama environment variables tune its own internal memory behavior:
- `OLLAMA_MAX_LOADED_MODELS=1` — never keep two models resident in RAM
  simultaneously, even if two different logical names get requested back to back
- `OLLAMA_NUM_PARALLEL=1` — process one inference request at a time, so RAM use
  per request stays predictable (the alternative is Ollama batching concurrent
  requests, which multiplies memory use)
- `OLLAMA_KEEP_ALIVE=5m` — unload the model from RAM after 5 minutes of no
  requests. This is what lets you leave the stack *running* without it
  permanently holding ~5-6GB of RAM — it only spikes during actual use.

### The gotcha: `cpus:` and thread count are two separate settings

`cpus: "5"` caps how much CPU *time* the container's cgroup gets — it does
**not** change what the process running inside sees when it asks "how many
cores does this machine have." Ollama (via llama.cpp) auto-detects the full
logical core count of the underlying machine and sizes its own thread pool to
match — completely unaware that only 5 cores' worth of scheduling time is
actually available to it. The result: if your machine reports, say, 14 logical
cores, Ollama spawns 14 threads to compete for a budget that only covers 5.
Those threads don't run proportionally slower — they thrash, constantly
context-switching and getting throttled, which is dramatically worse than
running the correct number of threads cleanly. In practice this showed up as
inference dropping to a small fraction of a token per second, looking
indistinguishable from "hung" even though the container was actively (and
wastefully) burning CPU the whole time.

The fix is `OLLAMA_NUM_THREAD` in `.env`, passed as `options.num_thread` on
every request in `gateway/app/ollama_client.py` — it tells llama.cpp exactly
how many threads to use, and it must be kept equal to (or a little under) the
`cpus:` value in `docker-compose.yml`. If you ever change one, change the
other. This is a genuinely easy trap to fall into with any containerized
CPU-bound workload, not just Ollama — cgroup quotas and a process's own
runtime-detected parallelism are configured completely independently unless
you deliberately wire them together, as we just did here.

### A second gotcha: Ollama silently shrinks the context window too

The whole reason Qwen2.5-7B-Instruct was chosen over Meditron was its 32,768
token context window — enough to hand it weeks of health data in one request.
But Ollama does not use a model's full trained context by default; it silently
runs with a much smaller window (commonly 4096 tokens) unless a request
explicitly asks for more via `options.num_ctx`. The server log makes this
visible if you know to look: `n_ctx_seq (4096) < n_ctx_train (32768) -- the
full capacity of the model will not be utilized`. Left unfixed, this would
have quietly capped you at roughly the same context ceiling as the domain
model we deliberately moved away from — defeating the actual point of the
choice.

The fix mirrors the thread one: `OLLAMA_NUM_CTX` in `.env` (default `16384`,
a deliberate middle ground rather than the full 32768) is passed as
`options.num_ctx` alongside `num_thread`. The tradeoff to know when tuning
this: a larger context window means a larger KV cache held in RAM (roughly
proportional to context length) and slower prompt processing for long inputs
(the model has to read every token before it can start generating), so raise
it based on how much data you actually need to fit, not just to the model's
theoretical maximum.

### A tried-and-reverted experiment: `num_batch`

`num_batch` controls how many prompt tokens llama.cpp processes together in
one pass during prefill — in theory, larger batches improve CPU cache/SIMD
utilization and speed up prompt processing (never generation, which is always
sequential one-token-at-a-time). Worth recording as a negative result: on
this hardware, doubling it from Ollama's own auto-selected value (1024, at
`OLLAMA_NUM_CTX=16384`) to 2048 measured *slightly worse* prompt-processing
throughput (30.26 → 29.34 tokens/sec) while doubling the compute buffer's RAM
use (310 MiB → 620 MiB) — a clear no-benefit-for-real-cost result, so it was
reverted rather than kept "just in case." The lesson isn't "num_batch never
matters" — it's that Ollama's automatic default was already reasonably tuned
for this context size, and blindly overriding a value you haven't measured
against can make things worse, not better. Always A/B against the same
payload before keeping a tuning change.

## 5. Cloudflare Tunnel — why this is safer than port forwarding

The traditional way to expose a home server is: forward a port on your router
(e.g. 443) to your PC's local IP, then use dynamic DNS since home IPs change.
The problem: your router is now *listening* for inbound internet connections on
that port, permanently, which means anyone can find it (mass internet scanners
find open ports within minutes of them appearing) and start probing it for
vulnerabilities — 24/7, whether you're using the server or not.

`cloudflared` inverts this: it runs inside your network and makes an
**outbound** connection to Cloudflare's edge, then holds that connection open.
Cloudflare routes public requests for `api.yourdomain.com` down that existing
outbound connection. Your router never accepts anything inbound — there is
nothing to scan or find, because nothing is listening on your public IP at all.
This is a structural difference, not just convenience: it removes an entire
category of attack (anything that starts with "find an open port").

The tradeoff: Cloudflare sits in the middle of every request (it can see
traffic, though it's still end-to-end TLS-terminated appropriately for a
personal project). For a personal/small-scale API this is a reasonable and
common tradeoff — it's what services like Tailscale Funnel and ngrok also do,
for the same reason.

### The 100-second wall, and why `/v1/jobs` exists

Cloudflare's free plan enforces a hard, non-configurable 100-second timeout on
every proxied request — confirmed against
[Cloudflare's own connection-limits docs](https://developers.cloudflare.com/fundamentals/reference/connection-limits/).
There's no setting on our side that changes this. Real `insights` requests
measured during development took 6-9 minutes end to end — meaning a
synchronous `POST /v1/chat` call for `insights` will *always* be killed by
Cloudflare's edge with a `524` error once this goes through the public tunnel,
regardless of how fast the local machine is that day.

Raising our own gateway-to-Ollama timeout (`_CHAT_TIMEOUT_SECONDS` in
`ollama_client.py`) was necessary to stop the *local* gateway from cutting
requests off early, but it does nothing for this external limit. The only
real fix is architectural: don't hold one HTTP connection open for the whole
duration at all.

`POST /v1/jobs` returns a job ID in well under a second — comfortably inside
any timeout, local or Cloudflare's. The actual work happens afterward, in a
background worker loop (`gateway/app/job_worker.py`) that the calling app
polls via `GET /v1/jobs/{job_id}`. This also directly addresses a separate,
related problem: Ollama processes exactly one request at a time
(`OLLAMA_NUM_PARALLEL=1` — see Section 4), so a burst of concurrent
`insights` calls was always going to queue up somewhere. Making that queue
explicit (a SQLite table, a known position number) turns an invisible,
eventually-timing-out wait into a state the caller can actually see and
handle (`queued`, position 3 → `processing` → `done`).

**Why SQLite instead of an in-memory dict**: an in-memory job list would
vanish on every `docker compose restart gateway`, silently orphaning any
caller still polling for a job ID that no longer exists anywhere. SQLite on
the `gateway_data` volume survives that restart. On startup, any job still
marked `processing` is explicitly marked `failed` — it belonged to a worker
loop that no longer exists and will never resume, so a caller polling for it
should get a clear answer rather than wait forever. Jobs still `queued` are
left alone; nothing about them was actually lost, and the worker picks them
up again as soon as it starts.

**Why exactly one worker, not several**: since Ollama itself only processes
one request at a time regardless of how many we send it, running multiple
concurrent jobs from the gateway side would just mean several of our own
requests piling up inside Ollama's own internal queue — invisible to us,
and no faster in aggregate. A single consumer loop
(`job_worker.run_worker_loop`) claiming one job at a time keeps our own
`queue_position` numbers accurate and never sends Ollama more than the one
request it can actually work on.

**A second Ollama instance would not help either**, for the same reason
raising thread count further didn't scale linearly in Section 4: the
constraint is real CPU compute (specifically, the four fast P-cores), not the
number of server processes. Splitting the same fixed CPU budget across two
Ollama instances means two requests each run at roughly half speed instead of
one running at full speed — the same total work done, worse per-request
latency. The only ways to genuinely raise throughput are reducing the work
per request (Section 6.6's data pre-filtering) or acquiring more real compute
(a GPU, cloud burst-out, better hardware) — this project does neither today.

## 6. Authentication — Bearer tokens and timing attacks

Each of your apps gets its own API key (`API_KEYS=medicalapp:sk-xxx,...` in
`.env`). Requests send it as `Authorization: Bearer sk-xxx`.

The interesting detail is in `gateway/app/auth.py`: keys are compared with
`hmac.compare_digest()` instead of Python's `==`. Here's why that matters —
`==` on strings in most implementations returns `False` as soon as it finds the
*first* differing character, meaning a string that matches the first 5 characters
takes microscopically longer to reject than one that matches 0 characters. An
attacker who can send enough requests and measure response times precisely
enough could, in theory, recover your key one character at a time. This is
called a **timing attack**. `compare_digest` always takes the same amount of
time regardless of where (or whether) the strings differ, closing that channel.
It's a small detail, but it's the kind of thing that separates "looks secure"
from "is secure" — worth internalizing for any future auth code you write.

## 7. Rate limiting — why it's keyed by API key, not IP address

Every request that reaches your gateway arrives *through the Cloudflare
Tunnel*, which means from the gateway's point of view, many different real
callers can appear to come from similar Cloudflare-edge IP ranges. Rate
limiting by IP address (the naive default) would either lump unrelated apps
together or fail to distinguish abuse from one specific key. Instead,
`gateway/app/rate_limit.py` extracts the Bearer token itself and rate-limits on
that — each app you issue a key to gets its own independent budget
(`RATE_LIMIT_PER_MINUTE` in `.env`), regardless of network path.

## 8. Container hardening — defense in depth

Even with network isolation, containers get extra restrictions in
`docker-compose.yml`, on the principle that no single layer should be the only
thing standing between an attacker and your machine:

- **`cap_drop: [ALL]`** — Linux processes can be granted fine-grained
  "capabilities" beyond plain user permissions (e.g. `NET_RAW` for raw sockets,
  `SYS_PTRACE` for debugging other processes). Dropping all of them means even
  if an attacker got code execution inside a container, they don't have these
  extra system-level abilities.
- **`security_opt: [no-new-privileges:true]`** — prevents a process from gaining
  more privileges than it started with (blocks a class of privilege-escalation
  exploits that rely on setuid binaries).
- **Non-root user in the gateway's own `Dockerfile`** (`USER appuser`, uid
  10001) — this is a container *we* build, so we control exactly what user it
  runs as. (The `ollama` container is a vendor image that manages its own
  internal user/permissions for writing model files to its volume — we don't
  override that, but we still constrain it with the network isolation and
  capability dropping above.)
- **`read_only: true` + `tmpfs: [/tmp]`** on the gateway — its entire root
  filesystem is mounted read-only except a temporary in-memory `/tmp` and the
  single `gateway_data` volume at `/data` used for the job queue database
  (Section 5). There is no other persistent place inside the container for an
  attacker to write a backdoor even if they found a code execution bug,
  because any write attempt outside those two paths fails outright.

None of these alone would stop a determined, resourced attacker — that's not
the threat model for a personal project. Together, they mean a huge number of
generic, automated attack techniques simply don't work here, which is the
realistic bar worth clearing.

## 9. The clean stop/start lifecycle

`docker compose down` (what `stop.ps1` runs) does three things: stops every
container, **removes** those containers, and removes the network connecting
them. Compare this to `docker compose stop`, which merely pauses containers —
that would still hold their allocated resources. `down` is why nothing lingers:
there is no process, no held RAM, no held network after it runs. The one thing
it deliberately does *not* remove is the named volumes — `ollama_data`
(downloaded model weights) and `gateway_data` (the job queue database) —
both are inert disk space, not a running resource. Keeping `ollama_data`
means `start.ps1` doesn't re-download several gigabytes every time; keeping
`gateway_data` means job history isn't wiped by a routine restart. Note the
distinction from Section 5: any job that was actively `processing` at the
moment of a stop genuinely cannot resume (the worker running it is gone) and
gets marked `failed` on the next start — only the *record* of jobs survives,
not in-flight work.

Notice also that no service in `docker-compose.yml` has a `restart: unless-stopped`
policy — they're all `restart: "no"`. This is deliberate: if Docker Desktop
itself restarts (say, after a Windows update reboot), the stack should **not**
quietly come back on its own. It only runs when you explicitly run `start.ps1`.

## 10. Glossary

- **cgroups** — Linux kernel mechanism for limiting how much CPU/RAM/etc. a
  group of processes can use; how Docker enforces `mem_limit`/`cpus`.
- **Bridge network** — Docker's default virtual network type; containers on the
  same bridge network can reach each other by service name.
- **Bearer token** — a credential sent in an HTTP header (`Authorization: Bearer <token>`)
  that grants access; anyone holding it can use it, so it must be transmitted
  over HTTPS and kept secret (never put in a URL query string, never logged).
- **Quantization** (e.g. `Q4_K_M`) — compressing a model's numerical weights to
  use fewer bits per number, trading a small amount of output quality for a
  large reduction in RAM/disk use and faster CPU inference — what makes a 7B
  model practical to run without a GPU.
- **Timing attack** — an attack that infers secret data from how *long* an
  operation takes, rather than its direct output.
- **Outbound-only tunnel** — a connection initiated from inside your network
  out to a remote service, which the remote service then uses to route traffic
  back in — no inbound firewall rule required.

## 11. What you could build next, yourself

Now that you've seen the pattern, here's how you'd extend it without needing to
ask for help:
- **Add a new model**: edit `models_registry.yaml`, `ollama pull` the tag,
  restart the gateway. That's the whole exercise — try pulling
  `llama3.1:8b-instruct-q4_K_M` under a new key and comparing its answers
  against `insights` on the same input.
- **Per-model system prompts**: each registry entry can carry an optional
  `system_prompt`, auto-prepended to any request that doesn't already include
  its own system message (see `gateway/app/routes/chat.py`). This is what lets
  `insights` and `chat` share the same underlying weights
  (`qwen2.5:7b-instruct-q4_K_M`) but behave differently — the prompt, not the
  model, defines the behavior. Tuning "how should it reason about my data" is
  a YAML edit, not a code change.
- **Add streaming responses**: the `stream` field already exists in the
  request schema (`gateway/app/routes/chat.py`) and is rejected with a clear
  error today. Implementing it means changing `ollama_client.chat()` to consume
  Ollama's streamed NDJSON response and re-emit it as Server-Sent Events from
  the FastAPI route — a good next exercise once the base system feels solid.
- **Per-key usage dashboards**: the gateway already knows which app name made
  each request (`caller` in `chat.py`) — logging that with a timestamp and
  token count to a file or SQLite database is a small, self-contained addition.
