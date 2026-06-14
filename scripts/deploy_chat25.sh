#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────
# Chat 25 Deploy Script — Run on <build-host> (your Windows PC / WSL)
# Deploys camera_adapter.py to <broker-host> (Pi 4) and restarts the service.
# Also runs v4l2-ctl to check if Y16 is available on the TC001.
# ────────────────────────────────────────────────────────────────────────
set -euo pipefail

<broker-host>="pi@<pi-ip>"
<broker-host>_PATH="~/Presence"

echo "═══════════════════════════════════════════════════════════════"
echo " Chat 25 — TC001 Y16 + Tearing Fix Deploy"
echo "═══════════════════════════════════════════════════════════════"

# ── Step 1: Check Y16 support on TC001 BEFORE deploying ────────────
echo ""
echo "── Step 1: Checking TC001 V4L2 format support ──"
echo ""
ssh "$<broker-host>" "v4l2-ctl -d /dev/video1 --list-formats-ext 2>&1 || echo 'v4l2-ctl failed — install: sudo apt install v4l-utils'"
echo ""
echo "Look for 'Y16' or 'GREY16' in the output above."
echo "If Y16 is listed, the new code will unlock real radiometric temperature data."
echo ""

read -p "Continue with deploy? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    exit 0
fi

# ── Step 2: Copy updated camera_adapter.py to <broker-host> ──────────────
echo ""
echo "── Step 2: Deploying camera_adapter.py to <broker-host> ──"
scp sentinel/adapters/camera_adapter.py "$<broker-host>:$<broker-host>_PATH/sentinel/adapters/camera_adapter.py"
echo "✅ camera_adapter.py deployed"

# ── Step 3: Restart the camera adapter service ──────────────────────
echo ""
echo "── Step 3: Restarting sentinel-camera-adapter service ──"
ssh "$<broker-host>" "sudo systemctl stop sentinel-camera-adapter; sleep 1; sudo fuser -k -s 8089/tcp 2>/dev/null; sleep 1; sudo systemctl start sentinel-camera-adapter"
echo "✅ Service restarted"

# ── Step 4: Wait and check logs for Y16 confirmation ───────────────
echo ""
echo "── Step 4: Checking logs for Y16 mode (waiting 5s for startup) ──"
sleep 5
ssh "$<broker-host>" "journalctl -u sentinel-camera-adapter --since '30 seconds ago' --no-pager -n 30"

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo " Look for one of these in the logs above:"
echo "   ✅ 'Y16 radiometric mode CONFIRMED'  → Real temperature data!"
echo "   ⚠️  'Y16 requested but driver returned FOURCC ...'  → Fallback to YUYV"
echo "   ⚠️  'Y16 set returned False'  → Fallback to YUYV"
echo ""
echo " If Y16 failed, the YUYV luminance path still works (15-45°C estimated)."
echo " Check thermal snapshot: http://<pi-ip>:8089/snapshot/thermal.jpg"
echo "═══════════════════════════════════════════════════════════════"
