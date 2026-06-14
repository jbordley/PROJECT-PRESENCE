# ─────────────────────────────────────────────────────────────────────────
# deploy_all.ps1 — Deploy sentinel services to <keep-host> + <broker-host>
# Update the KEEP and <broker-host> variables with your deployment targets
#
# Usage:
#   .\scripts\deploy_all.ps1 -Action deploy    # sync code + install units
#   .\scripts\deploy_all.ps1 -Action start     # sync + install + start all
#   .\scripts\deploy_all.ps1 -Action status    # check service status
#   .\scripts\deploy_all.ps1 -Action sync      # just sync code
# ─────────────────────────────────────────────────────────────────────────

param(
    [ValidateSet("deploy","start","status","sync")]
    [string]$Action = "deploy"
)

$KEEP   = "<user>@<jetson-ip>"
$<broker-host> = "<user>@<pi-ip>"

$KEEP_SERVICES = @("sentinel-node-adapter","sentinel-fusion","sentinel-brain","sentinel-dashboard")
$<broker-host>_SERVICES = @("sentinel-camera-adapter")

function Write-Info($msg)  { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Err($msg)   { Write-Host " [ERR] $msg" -ForegroundColor Red }

# ── Sync code ───────────────────────────────────────────────────────────
function Sync-Code {
    Write-Info "Syncing sentinel/ to <keep-host>..."
    scp -r sentinel "${KEEP}:~/Presence/"
    if ($LASTEXITCODE -eq 0) { Write-Ok "<keep-host>: sentinel/ synced" } else { Write-Err "<keep-host> sync failed" }

    Write-Info "Syncing sentinel/ to <broker-host>..."
    scp -r sentinel "${<broker-host>}:~/Presence/"
    if ($LASTEXITCODE -eq 0) { Write-Ok "<broker-host>: sentinel/ synced" } else { Write-Err "<broker-host> sync failed" }

    Write-Info "Syncing config..."
    scp sentinel_config.json "${KEEP}:~/sentinel/sentinel_config.json" 2>$null
    scp sentinel_config.json "${<broker-host>}:~/sentinel/sentinel_config.json" 2>$null
}

# ── Deploy systemd units ───────────────────────────────────────────────
function Deploy-Units {
    Write-Info "Deploying systemd units to <keep-host>..."
    foreach ($svc in $KEEP_SERVICES) {
        scp "systemd/${svc}.service" "${KEEP}:/tmp/${svc}.service"
        ssh $KEEP "sudo cp /tmp/${svc}.service /etc/systemd/system/"
        if ($LASTEXITCODE -eq 0) { Write-Ok "${svc}.service installed" } else { Write-Err "${svc} install failed" }
    }
    ssh $KEEP "sudo systemctl daemon-reload"
    Write-Ok "<keep-host>: daemon-reload done"

    Write-Info "Deploying systemd units to <broker-host>..."
    foreach ($svc in $<broker-host>_SERVICES) {
        scp "systemd/${svc}.service" "${<broker-host>}:/tmp/${svc}.service"
        ssh $<broker-host> "sudo cp /tmp/${svc}.service /etc/systemd/system/"
        if ($LASTEXITCODE -eq 0) { Write-Ok "${svc}.service installed" } else { Write-Err "${svc} install failed" }
    }
    ssh $<broker-host> "sudo systemctl daemon-reload"
    Write-Ok "<broker-host>: daemon-reload done"
}

# ── Start services ─────────────────────────────────────────────────────
function Start-AllServices {
    Write-Info "Starting services on <keep-host> (in order)..."
    foreach ($svc in $KEEP_SERVICES) {
        ssh $KEEP "sudo systemctl enable ${svc} && sudo systemctl restart ${svc}"
        if ($LASTEXITCODE -eq 0) { Write-Ok "${svc} started" } else { Write-Err "${svc} failed to start" }
        Start-Sleep -Seconds 2  # stagger startup
    }

    Write-Info "Starting services on <broker-host>..."
    foreach ($svc in $<broker-host>_SERVICES) {
        ssh $<broker-host> "sudo systemctl enable ${svc} && sudo systemctl restart ${svc}"
        if ($LASTEXITCODE -eq 0) { Write-Ok "${svc} started" } else { Write-Err "${svc} failed to start" }
    }
}

# ── Status check ───────────────────────────────────────────────────────
function Show-Status {
    Write-Host ""
    Write-Info "=== <keep-host> (<jetson-ip>) ==="
    foreach ($svc in $KEEP_SERVICES) {
        $status = ssh $KEEP "systemctl is-active ${svc} 2>/dev/null"
        if ($status -eq "active") { Write-Ok "${svc}: active" } else { Write-Err "${svc}: ${status}" }
    }

    Write-Host ""
    Write-Info "=== <broker-host> (<pi-ip>) ==="
    foreach ($svc in $<broker-host>_SERVICES) {
        $status = ssh $<broker-host> "systemctl is-active ${svc} 2>/dev/null"
        if ($status -eq "active") { Write-Ok "${svc}: active" } else { Write-Err "${svc}: ${status}" }
    }

    Write-Host ""
    Write-Info "Dashboard:  http://<jetson-ip>:8080"
    Write-Info "Camera:     http://<pi-ip>:8089/snapshot/camera.jpg"
    Write-Info "Thermal:    http://<pi-ip>:8089/snapshot/thermal.jpg"
}

# ── Main ───────────────────────────────────────────────────────────────
Set-Location "C:\Users\<user>\Desktop\Presence"

switch ($Action) {
    "sync" {
        Sync-Code
    }
    "deploy" {
        Sync-Code
        Deploy-Units
        Write-Info "Deploy complete. Run with -Action start to enable and start services."
    }
    "start" {
        Sync-Code
        Deploy-Units
        Start-AllServices
        Show-Status
    }
    "status" {
        Show-Status
    }
}
