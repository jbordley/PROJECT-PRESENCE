#!/usr/bin/env bash
# deploy_restart.sh — Deploy latest source to <keep-host> and restart services
# Picks up: °F publishing, BME688 warmup detection, EMRF field fix,
#           room_config, LiDAR+thermal+camera identity gate,
#           narrative unknown override fix (15s freshness check)
#
# Usage: bash scripts/deploy_restart.sh

set -euo pipefail

KEEP_HOST="<user>@<jetson-ip>"
KEEP_DIR="~/Presence"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

echo "═══════════════════════════════════════════════════"
echo "  SENTINEL Deploy & Restart"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════"
echo ""

# ── Step 1: Sync source ──────────────────────────────────────────────────
echo "▸ Syncing source to <keep-host>..."
rsync -avz --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude 'node_modules' \
    --exclude 'firmware/.pio' \
    "$LOCAL_DIR/sentinel/" \
    "$KEEP_HOST:$KEEP_DIR/sentinel/" \
|| fail "rsync failed"
ok "Source synced"

# Also sync config
rsync -avz "$LOCAL_DIR/sentinel_config.json" "$KEEP_HOST:$KEEP_DIR/sentinel_config.json" 2>/dev/null && ok "Config synced" || warn "No config file to sync"

# ── Step 2: Clear .pyc cache on <keep-host> ─────────────────────────────────
echo ""
echo "▸ Clearing Python cache on <keep-host>..."
ssh "$KEEP_HOST" "find $KEEP_DIR -name '*.pyc' -delete && find $KEEP_DIR -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true"
ok "Cache cleared"

# ── Step 3: Restart services in order ────────────────────────────────────
echo ""
echo "▸ Restarting services..."

# 1. Node Adapter — picks up °F, BME688 warmup detection
echo "  [1/3] sentinel-node-adapter..."
ssh "$KEEP_HOST" "sudo systemctl restart sentinel-node-adapter" && ok "  node_adapter restarted" || warn "  node_adapter restart failed (may not be a systemd service)"

sleep 2

# 2. Fusion Service — picks up EMRF field fix, room_config, identity gate
echo "  [2/3] sentinel-fusion..."
ssh "$KEEP_HOST" "sudo systemctl restart sentinel-fusion" && ok "  fusion restarted" || warn "  fusion restart failed (may not be a systemd service)"

sleep 2

# 3. Brain Service — picks up narrative unknown override fix
echo "  [3/3] sentinel-brain..."
ssh "$KEEP_HOST" "sudo systemctl restart sentinel-brain" && ok "  brain restarted" || warn "  brain restart failed (may not be a systemd service)"

# ── Step 4: Verify services are running ──────────────────────────────────
echo ""
echo "▸ Verifying services (5s settle)..."
sleep 5

for svc in sentinel-node-adapter sentinel-fusion sentinel-brain; do
    STATUS=$(ssh "$KEEP_HOST" "systemctl is-active $svc 2>/dev/null || echo 'unknown'")
    if [ "$STATUS" = "active" ]; then
        ok "  $svc is active"
    else
        warn "  $svc status: $STATUS"
    fi
done

# ── Step 5: Quick log check ──────────────────────────────────────────────
echo ""
echo "▸ Recent logs (last 5 lines each):"
for svc in sentinel-node-adapter sentinel-fusion sentinel-brain; do
    echo ""
    echo "  ── $svc ──"
    ssh "$KEEP_HOST" "journalctl -u $svc --no-pager -n 5 2>/dev/null" || echo "  (no journalctl output)"
done

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Deploy complete. Verify dashboard for °F display."
echo "  ESP32 reflash for LD2450 distance fix is separate."
echo "═══════════════════════════════════════════════════"
