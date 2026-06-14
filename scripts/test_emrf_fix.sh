#!/usr/bin/env bash
# Quick test: sync adapter fix and run manually to verify EMRF
# Run from your local Presence directory:
#   bash scripts/test_emrf_fix.sh

KEEP="<user>@<jetson-ip>"

echo "=== Quick EMRF Fix Test ==="

# Sync just the adapter
echo "[1] Syncing adapter..."
rsync -avz sentinel/adapters/ "${KEEP}:/home/<user>/Presence/sentinel/adapters/"

# Kill existing adapter
echo "[2] Restarting adapter..."
ssh "$KEEP" 'pkill -f "python3 -m sentinel.adapters" 2>/dev/null || true'
sleep 1

# Run adapter in foreground with DEBUG logging (Ctrl+C to stop)
echo "[3] Running adapter (DEBUG mode) — watch for 'Salvaged truncated JSON' messages..."
echo "    EMRF publishes every 30s, so wait up to 35s."
echo "    Press Ctrl+C when satisfied."
echo ""
ssh "$KEEP" 'cd ~/Presence && PYTHONPATH=~/Presence python3 -m sentinel.adapters --mqtt-host <pi-ip> --log-level DEBUG'
