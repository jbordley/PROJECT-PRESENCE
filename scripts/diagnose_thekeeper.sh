#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────
# SENTINEL — <keep-host> Diagnostic Script
# Run on <keep-host> (<jetson-ip>) to fix module import issues
# Usage: bash ~/Presence/scripts/diagnose_<keep-host>er.sh
# ──────────────────────────────────────────────────────────────────────────

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJ_DIR="$HOME/Presence"

echo "═══════════════════════════════════════════════"
echo "  SENTINEL — <keep-host> Diagnostic"
echo "═══════════════════════════════════════════════"
echo ""

# 1. Check project directory exists
echo -e "${YELLOW}[1] Project directory${NC}"
if [ -d "$PROJ_DIR" ]; then
    echo -e "  ${GREEN}✓${NC} $PROJ_DIR exists"
else
    echo -e "  ${RED}✗${NC} $PROJ_DIR NOT FOUND"
    exit 1
fi

# 2. Check __init__.py chain
echo -e "${YELLOW}[2] __init__.py chain${NC}"
for f in sentinel/__init__.py sentinel/dashboard/__init__.py sentinel/fusion/__init__.py sentinel/adapters/__init__.py sentinel/brain/__init__.py sentinel/schemas/__init__.py; do
    if [ -f "$PROJ_DIR/$f" ]; then
        echo -e "  ${GREEN}✓${NC} $f"
    else
        echo -e "  ${RED}✗${NC} $f MISSING — creating empty"
        touch "$PROJ_DIR/$f"
    fi
done

# 3. Clear all __pycache__
echo -e "${YELLOW}[3] Clearing __pycache__${NC}"
CACHE_COUNT=$(find "$PROJ_DIR" -type d -name __pycache__ | wc -l)
find "$PROJ_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
echo -e "  ${GREEN}✓${NC} Cleared $CACHE_COUNT __pycache__ dirs"

# 4. Test Python imports
echo -e "${YELLOW}[4] Testing Python imports${NC}"
cd "$PROJ_DIR"
export PYTHONPATH="$PROJ_DIR"

python3 -c "import sentinel; print(f'  ✓ sentinel {sentinel.__version__}')" 2>&1 || echo -e "  ${RED}✗${NC} sentinel import failed"
python3 -c "from sentinel.dashboard.service import DashboardServer; print('  ✓ sentinel.dashboard.service')" 2>&1 || echo -e "  ${RED}✗${NC} dashboard import failed"
python3 -c "from sentinel.fusion.service import FusionService; print('  ✓ sentinel.fusion.service')" 2>&1 || echo -e "  ${RED}✗${NC} fusion import failed"
python3 -c "from sentinel.adapters.node_adapter import NodeAdapter; print('  ✓ sentinel.adapters.node_adapter')" 2>&1 || echo -e "  ${RED}✗${NC} node_adapter import failed"

# 5. Check dependencies
echo -e "${YELLOW}[5] Python dependencies${NC}"
for pkg in paho.mqtt.client uvicorn starlette; do
    python3 -c "import $pkg; print('  ✓ $pkg')" 2>&1 || echo -e "  ${RED}✗${NC} $pkg — install with: pip install ${pkg//./-}"
done

# 6. Check MQTT connectivity
echo -e "${YELLOW}[6] MQTT broker (<broker-host> <pi-ip>)${NC}"
if python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
s.connect(('<pi-ip>', 1883))
s.close()
print('  ✓ MQTT port 1883 reachable')
" 2>/dev/null; then
    true
else
    echo -e "  ${RED}✗${NC} Cannot reach MQTT broker at <pi-ip>:1883"
fi

# 7. Check config
echo -e "${YELLOW}[7] Config file${NC}"
CONFIG="$HOME/sentinel/sentinel_config.json"
if [ -f "$CONFIG" ]; then
    echo -e "  ${GREEN}✓${NC} $CONFIG exists"
    python3 -c "import json; c=json.load(open('$CONFIG')); print(f\"  MQTT host: {c.get('mqtt',{}).get('host','NOT SET')}\")"
else
    echo -e "  ${YELLOW}!${NC} No config file — will use defaults (MQTT=127.0.0.1)"
    echo -e "  ${YELLOW}!${NC} Creating config with correct MQTT host..."
    mkdir -p "$HOME/sentinel"
    cat > "$CONFIG" << 'CONF'
{
  "mqtt": {
    "host": "<pi-ip>",
    "port": 1883,
    "client_id_prefix": "sentinel"
  },
  "home_name": "home",
  "owner": "<user>"
}
CONF
    echo -e "  ${GREEN}✓${NC} Created $CONFIG with mqtt.host=<pi-ip>"
fi

echo ""
echo "═══════════════════════════════════════════════"
echo -e "${GREEN}Diagnostic complete.${NC}"
echo ""
echo "Quick-start commands:"
echo "  # Dashboard:"
echo "  cd ~/Presence && PYTHONPATH=~/Presence python3 -m sentinel.dashboard --mqtt-host <pi-ip> --port 8080"
echo ""
echo "  # Fusion:"
echo "  cd ~/Presence && PYTHONPATH=~/Presence python3 -m sentinel.fusion --mqtt-host <pi-ip>"
echo ""
echo "  # Node Adapter:"
echo "  cd ~/Presence && PYTHONPATH=~/Presence python3 -m sentinel.adapters --mqtt-host <pi-ip>"
echo "═══════════════════════════════════════════════"
