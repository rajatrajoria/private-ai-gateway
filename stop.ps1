# Fully stops and REMOVES all containers + the Docker network for this stack.
# Model weights (the ollama_data volume) are kept so next start.ps1 is fast.
# This is what actually frees RAM/CPU and drops the Cloudflare Tunnel connection.

# Warn about any async job that's still queued or actively processing -
# stopping now means "processing" jobs are lost (marked failed on next start,
# see TECHNICAL_OVERVIEW.md); "queued" ones survive but won't run until you
# start the stack again. Runs a real script file (not an inline -c one-liner)
# to sidestep PowerShell/docker-exec quoting issues entirely.
$pending = docker compose exec -T gateway python -m app.check_pending_jobs 2>$null

if ($pending) {
    Write-Host "Heads up - you have jobs that haven't finished:" -ForegroundColor Yellow
    Write-Host $pending -ForegroundColor Yellow
    Write-Host ""
}

docker compose down

Write-Host ""
Write-Host "Stack stopped. Verifying nothing is left running..." -ForegroundColor Cyan
docker compose ps

Write-Host ""
Write-Host "Model weights are preserved on disk (docker volume 'ollama_data') so the next start is fast."
Write-Host "Docker Desktop itself may still use a small amount of idle RAM in the background."
Write-Host "To free that too, quit Docker Desktop from its system tray icon - not required, just optional."
