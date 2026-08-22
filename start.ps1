# Brings the whole stack up: ollama + gateway + cloudflared.
# Safe to run repeatedly - docker compose reuses what's already there.

# --- Docker prerequisite checks ---
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Docker was not found on this machine." -ForegroundColor Red
    Write-Host "Install Docker Desktop: https://www.docker.com/products/docker-desktop/" -ForegroundColor Yellow
    Write-Host "(The installer handles enabling WSL2 for you on a fresh machine.)" -ForegroundColor Yellow
    exit 1
}

function Test-DockerRunning {
    docker info *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (Test-DockerRunning)) {
    Write-Host "Docker is installed but not running." -ForegroundColor Yellow

    # Docker Desktop on Windows needs WSL2. This is a best-effort check, not
    # an auto-install: enabling WSL2 requires an elevated shell and usually a
    # reboot, which this script won't do on its own - that's a system-level
    # change you should run and confirm yourself.
    $wslOk = $true
    try {
        wsl --status *> $null
        if ($LASTEXITCODE -ne 0) { $wslOk = $false }
    } catch {
        $wslOk = $false
    }

    if (-not $wslOk) {
        Write-Host "WSL2 does not appear to be installed or enabled." -ForegroundColor Red
        Write-Host "Run this in an ELEVATED PowerShell, restart your machine, then re-run this script:" -ForegroundColor Yellow
        Write-Host "    wsl --install" -ForegroundColor Cyan
        exit 1
    }

    Write-Host "Attempting to start Docker Desktop..." -ForegroundColor Yellow
    $dockerDesktopPath = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerDesktopPath) {
        Start-Process $dockerDesktopPath
    } else {
        Write-Host "Could not find Docker Desktop.exe at the default install path - start it manually from the Start menu." -ForegroundColor Yellow
    }

    Write-Host "Waiting for the Docker engine to come up (this can take a minute on a cold start)..."
    $dockerAttempts = 0
    $dockerReady = $false
    while ($dockerAttempts -lt 60) {
        Start-Sleep -Seconds 2
        if (Test-DockerRunning) {
            $dockerReady = $true
            break
        }
        $dockerAttempts++
    }

    if (-not $dockerReady) {
        Write-Host "Docker engine did not come up in time. Start Docker Desktop manually and re-run this script." -ForegroundColor Red
        exit 1
    }
    Write-Host "Docker engine is up." -ForegroundColor Green
}
# --- end Docker prerequisite checks ---

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
