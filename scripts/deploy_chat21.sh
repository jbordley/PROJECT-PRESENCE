#!/usr/bin/env bash
# ============================================================
# Chat 21 Deployment — EMRF fix + fusion + systemd
# Run from your local machine (where Presence repo lives):
#   bash scripts/deploy_chat21.sh
# ============================================================
set -euo pipefail

KEEP="<user>@<jetson-ip>"
REMOTE_DIR="/home/<user>/Presence"

echo "=== Chat 21 Deploy ==="
echo ""

# 1. Sync updated files to <keep-host>
echo "[1/5] Syncing updated files..."
rsync -avz --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'sentinel_node/' \
    ./ "${KEEP}:${REMOTE_DIR}/"
echo "  ✓ Files synced"

# 2. Kill any manually-running adapter/dashboard processes
echo ""
echo "[2/5] Stopping manual processes..."
ssh "$KEEP" 'pkill -f "python3 -m sentinel" 2>/dev/null || true'
sleep 1
echo "  ✓ Manual processes stopped"

# 3. Install systemd unit files
echo ""
echo "[3/5] Installing systemd services..."
ssh "$KEEP" "bash -s" <<'REMOTE'
set -e
sudo cp ~/Presence/systemd/sentinel-node-adapter.service /etc/systemd/system/
sudo cp ~/Presence/systemd/sentinel-fusion.service /etc/systemd/system/
sudo cp ~/Presence/systemd/sentinel-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sentinel-node-adapter sentinel-fusion sentinel-dashboard
echo "  ✓ Units installed and enabled"
REMOTE

# 4. Start services (order matters: adapter → fusion → dashboard)
echo ""
echo "[4/5] Starting services..."
ssh "$KEEP" "bash -s" <<'REMOTE'
set -e
sudo systemctl restart sentinel-node-adapter
sleep 2
sudo systemctl restart sentinel-fusion
sleep 1
sudo systemctl restart sentinel-dashboard
echo "  ✓ All services started"
REMOTE

# 5. Verify
echo ""
echo "[5/5] Verifying..."
ssh "$KEEP" "bash -s" <<'REMOTE'
echo ""
echo "=== Service Status ==="
for svc in sentinel-node-adapter sentinel-fusion sentinel-dashboard; do
    status=$(systemctl is-active "$svc" 2>/dev/null || echo "inactive")
    printf "  %-30s %s\n" "$svc" "$status"
done

echo ""
echo "=== Dashboard ==="
echo "  http://<jetson-ip>:8080"

echo ""
echo "=== Recent adapter logs (watching for EMRF) ==="
sudo journalctl -u sentinel-node-adapter --no-pager -n 20

echo ""
echo "=== MQTT test: listening for emrf/raw (10s) ==="
timeout 35 mosquitto_sub -h <pi-ip> -t 'sentinel/sensors/office/emrf/raw' -C 1 -W 35 2>/dev/null && echo "  ✓ EMRF data flowing!" || echo "  ⚠ No EMRF message in 35s (wait for next 30s publish cycle)"
REMOTE

echo ""
echo "=== Deploy complete ==="
echo "Next: flash firmware with OTA for buffer fix (optional — adapter salvages truncated JSON now)"
