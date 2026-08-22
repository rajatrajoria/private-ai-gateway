#!/usr/bin/env bash
# Mac/Linux equivalent of stop.ps1 — same behavior, bash instead of PowerShell.
# Fully stops and REMOVES all containers + the Docker network for this stack.
# Model weights (the ollama_data volume) are kept so next start.sh is fast.
# This is what actually frees RAM/CPU and drops the Cloudflare Tunnel connection.
set -uo pipefail
cd "$(dirname "$0")"

# Warn about any async job that's still queued or actively processing -
# stopping now means "processing" jobs are lost (marked failed on next
# start, see TECHNICAL_OVERVIEW.md); "queued" ones survive but won't run
# until you start the stack again.
pending=$(docker compose exec -T gateway python -m app.check_pending_jobs 2>/dev/null)

if [ -n "$pending" ]; then
    echo "Heads up - you have jobs that haven't finished:"
    echo "$pending"
    echo
fi

docker compose down

echo
echo "Stack stopped. Verifying nothing is left running..."
docker compose ps

echo
echo "Model weights are preserved on disk (docker volume 'ollama_data') so the next start is fast."
if [ "$(uname -s)" = "Darwin" ]; then
    echo "Docker Desktop itself may still use a small amount of idle RAM in the background."
    echo "To free that too, quit Docker Desktop from its menu bar icon - not required, just optional."
else
    echo "The Docker daemon itself may still use a small amount of idle RAM in the background."
    echo "To free that too: sudo systemctl stop docker - not required, just optional."
fi
