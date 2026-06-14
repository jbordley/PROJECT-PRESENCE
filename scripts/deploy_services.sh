#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────
# SENTINEL — Deploy & Install Services on <keep-host>
# Run FROM your dev machine (or <keep-host> itself)
#
# Usage (from dev machine):
#   scp -r ~/Presence/systemd ~/Presence/scripts ~/Presence/sentinel_config.json <user>@<jetson-ip>:~/Presence/
#   ssh <user>@<jetson-ip> 'bash ~/Presence/scripts/deploy_services.sh'
#
# Usage (on <keep-host> directly):
#   bash ~/Presence/scripts/deploy_services.sh
# ──────────────────────────────────────────────────────────────────────────

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJ_DIR="$HOME/Presence"
SYSTEMD_SRC="$PROJ_DIR/systemd"
SYSTEMD_DST="/etc/systemd/system"
CONFIG_SRC="$PROJ_DIR/sentinel_config.json"
CONFIG_DST="$HOME/sentinel/sentinel_config.json"

echo "═══════════════════════════════════════════════"
echo "  SENTINEL — Service Deployment"
echo "═══════════════════════════════════════════════"
echo ""

# 1. Run diagnostics first
echo -e "${YELLOW}[1] Running diagnostics...${NC}"
bash "$PROJ_DIR/scripts/diagnose_<keep-host>er.sh"
echo ""

# 2. Install config
echo -e "${YELLOW}[2] Installing config${NC}"
mkdir -p "$HOME/sentinel"
if [ -f "$CONFIG_SRC" ]; then
    cp "$CONFIG_SRC" "$CONFIG_DST"
    echo -e "  ${GREEN}✓${NC} Config installed to $CONFIG_DST"
else
    echo -e "  ${YELLOW}!${NC} No sentinel_config.json in project root — using existing or diagnostics-created config"
fi

# 3. Install systemd unit files
echo -e "${YELLOW}[3] Installing systemd unit files${NC}"
SERVICES=("sentinel-node-adapter" "sentinel-fusion" "sentinel-dashboard")

for svc in "${SERVICES[@]}"; do
    if [ -f "$SYSTEMD_SRC/$svc.service" ]; then
        sudo cp "$SYSTEMD_SRC/$svc.service" "$SYSTEMD_DST/"
        echo -e "  ${GREEN}✓${NC} $svc.service installed"
    else
        echo -e "  ${RED}✗${NC} $svc.service not found in $SYSTEMD_SRC"
    fi
done

# 4. Reload systemd
echo -e "${YELLOW}[4] Reloading systemd${NC}"
sudo systemctl daemon-reload
echo -e "  ${GREEN}✓${NC} systemd reloaded"

# 5. Enable services (don't start yet)
echo -e "${YELLOW}[5] Enabling services${NC}"
for svc in "${SERVICES[@]}"; do
    sudo systemctl enable "$svc" 2>/dev/null && echo -e "  ${GREEN}✓${NC} $svc enabled" || echo -e "  ${RED}✗${NC} $svc enable failed"
done

echo ""
echo "═══════════════════════════════════════════════"
echo -e "${GREEN}Deployment complete.${NC}"
echo ""
echo "Start services in order:"
echo "  sudo systemctl start sentinel-node-adapter"
echo "  sudo systemctl start sentinel-fusion"
echo "  sudo systemctl start sentinel-dashboard"
echo ""
echo "Or start all at once:"
echo "  sudo systemctl start sentinel-node-adapter sentinel-fusion sentinel-dashboard"
echo ""
echo "Check status:"
echo "  sudo systemctl status sentinel-node-adapter sentinel-fusion sentinel-dashboard"
echo ""
echo "View logs:"
echo "  journalctl -u sentinel-dashboard -f"
echo "  journalctl -u sentinel-fusion -f"
echo "  journalctl -u sentinel-node-adapter -f"
echo ""
echo "Dashboard will be at: http://<jetson-ip>:8080"
echo "═══════════════════════════════════════════════"
