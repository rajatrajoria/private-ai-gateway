# Brings the whole stack up: ollama + gateway + cloudflared.
# Safe to run repeatedly - docker compose reuses what's already there.

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - edit it with real API keys before exposing this publicly." -ForegroundColor Yellow
}

$envContent = Get-Content ".env" -Raw
$hasTunnelToken = $envContent -match "TUNNEL_TOKEN=\s*\S"

if ($hasTunnelToken) {
    $composeArgs = @("-f", "docker-compose.yml", "-f", "docker-compose.tunnel.yml")
    Write-Host "TUNNEL_TOKEN is set - using your stable, named Cloudflare Tunnel." -ForegroundColor Green
} else {
    $composeArgs = @("-f", "docker-compose.yml")
    Write-Host "TUNNEL_TOKEN is empty - cloudflared will start a free Quick Tunnel instead." -ForegroundColor Yellow
    Write-Host "No account or domain needed, but its *.trycloudflare.com URL changes every restart." -ForegroundColor Yellow
    Write-Host "Once it's up, find the URL with: docker compose logs cloudflared" -ForegroundColor Yellow
    Write-Host "See cloudflared/README.md when you're ready to switch to a permanent domain." -ForegroundColor Yellow
}

docker compose @composeArgs up -d --build

Write-Host "Waiting for the gateway to become healthy..."
$attempts = 0
$healthy = $false
while ($attempts -lt 30) {
    Start-Sleep -Seconds 2
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/healthz" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) {
            $healthy = $true
            break
        }
    } catch {}
    $attempts++
}

if ($healthy) {
    Write-Host "Gateway is up: http://127.0.0.1:8000" -ForegroundColor Green
} else {
    Write-Host "Gateway did not become healthy in time. Run 'docker compose logs gateway' to see why." -ForegroundColor Red
}

if (-not $hasTunnelToken) {
    Write-Host "Looking for your public Quick Tunnel URL..."
    Start-Sleep -Seconds 3
    docker compose logs cloudflared 2>&1 | Select-String "trycloudflare.com"
}

docker compose @composeArgs ps
