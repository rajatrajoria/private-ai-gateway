#!/usr/bin/env bash
# Mac/Linux equivalent of start.ps1 — same behavior, bash instead of PowerShell.
# Brings the whole stack up: ollama + gateway + cloudflared. Safe to run
# repeatedly - docker compose reuses what's already there.
set -uo pipefail
cd "$(dirname "$0")"

OS="$(uname -s)"

# --- Docker prerequisite checks ---
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker was not found on this machine." >&2
    if [ "$OS" = "Darwin" ]; then
        echo "Install Docker Desktop: https://www.docker.com/products/docker-desktop/" >&2
    else
        echo "Install Docker Engine for your distro: https://docs.docker.com/engine/install/" >&2
    fi
    exit 1
fi

docker_running() {
    docker info >/dev/null 2>&1
}

if ! docker_running; then
    echo "Docker is installed but not running. Attempting to start it..."
    if [ "$OS" = "Darwin" ]; then
        open -a Docker 2>/dev/null || echo "Could not launch Docker Desktop automatically - start it manually." >&2
    elif command -v systemctl >/dev/null 2>&1; then
        # Most Linux distros run the Docker daemon as a systemd service.
        # Needs sudo since starting a system service is a privileged action -
        # this will prompt for a password if one is required, same as if you
        # ran it by hand.
        sudo systemctl start docker 2>/dev/null || echo "Could not start the docker service automatically - start it manually (e.g. 'sudo systemctl start docker')." >&2
    else
        echo "Don't know how to auto-start Docker on this system (no systemd found) - start it manually." >&2
    fi

    echo "Waiting for the Docker engine to come up (this can take a minute on a cold start)..."
    docker_ready=false
    for _ in $(seq 1 60); do
        sleep 2
        if docker_running; then
            docker_ready=true
            break
        fi
    done

    if [ "$docker_ready" != true ]; then
        echo "Docker engine did not come up in time. Start it manually and re-run this script." >&2
        exit 1
    fi
    echo "Docker engine is up."
fi
# --- end Docker prerequisite checks ---

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example - edit it with real API keys before exposing this publicly."
fi

COMPOSE_ARGS=(-f docker-compose.yml)
if grep -qE "TUNNEL_TOKEN=[[:space:]]*[^[:space:]]" .env 2>/dev/null; then
    COMPOSE_ARGS+=(-f docker-compose.tunnel.yml)
    HAS_TUNNEL_TOKEN=true
    echo "TUNNEL_TOKEN is set - using your stable, named Cloudflare Tunnel."
else
    HAS_TUNNEL_TOKEN=false
    echo "TUNNEL_TOKEN is empty - cloudflared will start a free Quick Tunnel instead."
    echo "No account or domain needed, but its *.trycloudflare.com URL changes every restart."
    echo "Once it's up, find the URL with: docker compose logs cloudflared"
    echo "See cloudflared/README.md when you're ready to switch to a permanent domain."
fi

docker compose "${COMPOSE_ARGS[@]}" up -d --build

echo "Waiting for the gateway to become healthy..."
healthy=false
for _ in $(seq 1 30); do
    sleep 2
    if curl -fsS "http://127.0.0.1:8000/healthz" >/dev/null 2>&1; then
        healthy=true
        break
    fi
done

if [ "$healthy" = true ]; then
    echo "Gateway is up: http://127.0.0.1:8000"
else
    echo "Gateway did not become healthy in time. Run 'docker compose logs gateway' to see why." >&2
fi

if [ "$HAS_TUNNEL_TOKEN" != true ]; then
    echo "Looking for your public Quick Tunnel URL..."
    sleep 3
    docker compose logs cloudflared 2>&1 | grep -i trycloudflare || true
fi

docker compose "${COMPOSE_ARGS[@]}" ps
