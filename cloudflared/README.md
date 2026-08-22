# Setting up the Cloudflare Tunnel

## Don't have a domain yet? You already have a public URL

If `TUNNEL_TOKEN` is blank in `.env`, `start.ps1`/`start.sh` automatically starts a free
**Quick Tunnel** instead — no Cloudflare account, no domain, no cost. It
prints a real, working `https://<random-words>.trycloudflare.com` URL that's
genuinely reachable from anywhere on the internet, found with:

```bash
docker compose logs cloudflared
```

This is enough to test that your already-hosted app can actually reach this
gateway. The catch: that URL is randomly generated and **changes every time
the tunnel restarts** — fine for testing, not something to hardcode into a
production app. When you're ready for a permanent address, follow the steps
below; nothing else about the setup changes, `start.ps1`/`start.sh` detects
`TUNNEL_TOKEN` and switches over automatically.

## The permanent setup (one-time, needs your own domain)

This part needs your own Cloudflare account and a domain you control — not
scripted, since it's inherently tied to your own account.

## Prerequisites
- A free Cloudflare account
- A domain added to that Cloudflare account (any registrar works — Cloudflare just
  needs to manage its DNS). A cheap domain (~$10/year) is enough; you don't need
  Cloudflare to be your registrar.

## Steps

1. Go to the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/) →
   **Networks → Tunnels → Create a tunnel**.
2. Choose **Cloudflared** as the connector type, give it a name (e.g. `private-ai-gateway`).
3. Cloudflare shows you an install command containing a token — you only need the
   token itself (a long string after `--token`). Copy it.
4. Paste that token into your `.env` file as `TUNNEL_TOKEN=...`.
5. Still in the dashboard, add a **Public Hostname**:
   - Subdomain: whatever you want, e.g. `api`
   - Domain: your domain, e.g. `yourdomain.com` → gives you `api.yourdomain.com`
   - Service type: `HTTP`
   - URL: `gateway:8000` (this is the Docker service name — cloudflared reaches it
     over the internal Docker network, not localhost, because they're in the same
     `docker-compose.yml`)
6. Save. Run `start.ps1`/`start.sh` in the project root — the `cloudflared` container will
   pick up `TUNNEL_TOKEN` from `.env` and connect automatically.
7. Test from *outside* your home network (phone on mobile data, for example):
   ```
   curl -H "Authorization: Bearer <one of your API_KEYS>" https://api.yourdomain.com/v1/models
   ```

## Why no port forwarding, no dynamic DNS, no firewall rule

`cloudflared` only makes *outbound* connections to Cloudflare's edge — your router
never has to accept an inbound connection at all. There's nothing to port-scan or
attack on your home IP, because nothing is listening there. This is the main
reason Cloudflare Tunnel was chosen over the traditional "forward port 443 to my
PC" approach — see `TECHNICAL_OVERVIEW.md` for the full reasoning.

## Turning it off

Public access exists only while the stack is running. Run `stop.ps1`/`stop.sh`
in the project root and the tunnel connection drops immediately —
`api.yourdomain.com` will simply stop resolving to anything until you
`start.ps1`/`start.sh` again.
