#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────
# OTA Flash Node-01 — Run from <build-host> (where PlatformIO is installed)
# Flashes the firmware with 4096B MQTT buffer, 3072B serialize buffer,
# and top-20 device reporting to fix the corrupted EMRF JSON.
# ────────────────────────────────────────────────────────────────────────
set -euo pipefail

NODE_HOST="sentinel-node-01.local"
FIRMWARE_DIR="sentinel_node"

echo "═══════════════════════════════════════════════════════════════"
echo " OTA Flash — sentinel-node-01"
echo "═══════════════════════════════════════════════════════════════"

# ── Step 1: Verify node is reachable via mDNS ──────────────────────
echo ""
echo "── Step 1: Checking if $NODE_HOST is reachable ──"
if ping -c 2 -W 2 "$NODE_HOST" > /dev/null 2>&1; then
    echo "✅ $NODE_HOST is reachable"
else
    echo "❌ Cannot reach $NODE_HOST"
    echo "   Make sure node-01 is powered on and connected to WiFi."
    echo "   Try: ping <esp32-ip> (static IP)"
    exit 1
fi

# ── Step 2: Build firmware ─────────────────────────────────────────
echo ""
echo "── Step 2: Building firmware ──"
cd "$FIRMWARE_DIR"
pio run
echo "✅ Build successful"

# ── Step 3: OTA Flash ──────────────────────────────────────────────
echo ""
echo "── Step 3: Flashing via OTA to $NODE_HOST ──"
pio run -t upload --upload-port "$NODE_HOST"
echo "✅ OTA flash complete"

# ── Step 4: Wait for reboot and verify ─────────────────────────────
echo ""
echo "── Step 4: Waiting for node to reboot (10s) ──"
sleep 10

if ping -c 2 -W 2 "$NODE_HOST" > /dev/null 2>&1; then
    echo "✅ Node-01 is back online"
else
    echo "⚠️  Node-01 not responding yet — may need more time to reconnect to WiFi"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo " After flash, verify:"
echo "   1. Dashboard EMRF panel shows clean device data (no truncated JSON)"
echo "   2. Serial monitor: pio device monitor (check heap, MQTT connected)"
echo "   3. MQTT: mosquitto_sub -h <pi-ip> -t 'home/sentinel/node-01/#' -v"
echo "═══════════════════════════════════════════════════════════════"
