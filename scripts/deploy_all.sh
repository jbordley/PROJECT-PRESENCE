#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────
# deploy_all.sh — Deploy sentinel services to <keep-host> + <broker-host>
# Run from <build-host> (Windows/WSL) or any machine with SSH access.
#
# Usage:
#   ./scripts/deploy_all.sh [--sync-only] [--start] [--status]
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

KEEP_HOST="<user>@<jetson-ip>"
<broker-host>_HOST="<user>@<pi-ip>"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC} $1"; }

# ── Parse args ──────────────────────────────────────────────────────────
ACTION="${1:-deploy}"

# ── Sync code to both machines ──────────────────────────────────────────
sync_code() {
    info "Syncing sentinel/ package to <keep-host>..."
    scp -r "$PROJECT_DIR/sentinel" "${KEEP_HOST}:~/Presence/" 2>/dev/null && ok "<keep-host>: sentinel/ synced" || err "<keep-host>: sync failed"

    info "Syncing sentinel/ package to <broker-host>..."
    scp -r "$PROJECT_DIR/sentinel" "${<broker-host>_HOST}:~/Presence/" 2>/dev/null && ok "<broker-host>: sentinel/ synced" || err "<broker-host>: sync failed"

    info "Syncing config to <keep-host>..."
    scp "$PROJECT_DIR/sentinel_config.json" "${KEEP_HOST}:~/sentinel/sentinel_config.json" 2>/dev/null && ok "<keep-host>: config synced" || err "<keep-host>: config sync failed"

    info "Syncing config to <broker-host>..."
    scp "$PROJECT_DIR/sentinel_config.json" "${<broker-host>_HOST}:~/sentinel/sentinel_config.json" 2>/dev/null && ok "<broker-host>: config synced" || err "<broker-host>: config sync failed"
}

# ── Deploy systemd units ───────────────────────────────────────────────
deploy_systemd() {
    # <keep-host> services: node-adapter, fusion, brain, dashboard
    info "Deploying systemd units to <keep-host>..."
    for svc in sentinel-node-adapter sentinel-fusion sentinel-brain sentinel-dashboard; do
        scp "$PROJECT_DIR/systemd/${svc}.service" "${KEEP_HOST}:/tmp/${svc}.service" 2>/dev/null
        ssh "$KEEP_HOST" "sudo cp /tmp/${svc}.service /etc/systemd/system/ && sudo systemctl daemon-reload" 2>/dev/null
        ok "<keep-host>: ${svc}.service installed"
    done

    # <broker-host> services: camera-adapter
    info "Deploying systemd units to <broker-host>..."
    scp "$PROJECT_DIR/systemd/sentinel-camera-adapter.service" "${<broker-host>_HOST}:/tmp/sentinel-camera-adapter.service" 2>/dev/null
    ssh "$<broker-host>_HOST" "sudo cp /tmp/sentinel-camera-adapter.service /etc/systemd/system/ && sudo systemctl daemon-reload" 2>/dev/null
    ok "<broker-host>: sentinel-camera-adapter.service installed"
}

# ── Enable and start services ──────────────────────────────────────────
start_services() {
    info "Starting services on <keep-host> (order: adapter → fusion → brain → dashboard)..."
    for svc in sentinel-node-adapter sentinel-fusion sentinel-brain sentinel-dashboard; do
        ssh "$KEEP_HOST" "sudo systemctl enable ${svc} && sudo systemctl restart ${svc}" 2>/dev/null
        ok "<keep-host>: ${svc} started"
    done

    info "Starting camera adapter on <broker-host>..."
    ssh "$<broker-host>_HOST" "sudo systemctl enable sentinel-camera-adapter && sudo systemctl restart sentinel-camera-adapter" 2>/dev/null
    ok "<broker-host>: sentinel-camera-adapter started"
}

# ── Status check ───────────────────────────────────────────────────────
show_status() {
    echo ""
    info "=== <keep-host> (<jetson-ip>) ==="
    for svc in sentinel-node-adapter sentinel-fusion sentinel-brain sentinel-dashboard; do
        STATUS=$(ssh "$KEEP_HOST" "systemctl is-active ${svc} 2>/dev/null" || echo "inactive")
        if [ "$STATUS" = "active" ]; then
            ok "${svc}: ${STATUS}"
        else
            err "${svc}: ${STATUS}"
        fi
    done

    echo ""
    info "=== <broker-host> (<pi-ip>) ==="
    STATUS=$(ssh "$<broker-host>_HOST" "systemctl is-active sentinel-camera-adapter 2>/dev/null" || echo "inactive")
    if [ "$STATUS" = "active" ]; then
        ok "sentinel-camera-adapter: ${STATUS}"
    else
        err "sentinel-camera-adapter: ${STATUS}"
    fi

    echo ""
    info "Dashboard: http://<jetson-ip>:8080"
    info "Snapshots: http://<pi-ip>:8089/snapshot/camera.jpg"
    info "           http://<pi-ip>:8089/snapshot/thermal.jpg"
}

# ── OTA flash helper ───────────────────────────────────────────────────
flash_ota() {
    NODE="${2:-node-01}"
    info "OTA flashing ${NODE}..."
    info "Looking for sentinel-${NODE}.local via mDNS..."
    cd "$PROJECT_DIR/sentinel_node"
    pio run -t upload --upload-port "sentinel-${NODE}.local"
    ok "OTA flash complete for ${NODE}"
}

# ── Main ───────────────────────────────────────────────────────────────
case "$ACTION" in
    deploy)
        sync_code
        deploy_systemd
        info "Deploy complete. Run with --start to enable and start services."
        ;;
    --sync-only)
        sync_code
        ;;
    --start)
        sync_code
        deploy_systemd
        start_services
        show_status
        ;;
    --status)
        show_status
        ;;
    --flash-ota)
        flash_ota "$@"
        ;;
    *)
        echo "Usage: $0 [deploy|--sync-only|--start|--status|--flash-ota <node-id>]"
        exit 1
        ;;
esac
