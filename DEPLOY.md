# Sentinel Deploy Cheat Sheet

All commands run from <build-host>: `C:\Users\<user>\Desktop\Presence`

## Hosts

| Host | Role | IP |
|---|---|---|
| <build-host> | Dev PC (source of truth) | — |
| <keep-host> | Jetson — fusion, brain, dashboard, node adapter | <keep-ip> |
| <broker-host> | Pi 4 — MQTT broker, camera adapter | <broker-ip> |

## SCP to <keep-host> (Jetson)

```powershell
# Dashboard
scp sentinel\dashboard\index.html <user>@<keep-ip>:~/Presence/sentinel/dashboard/index.html
scp sentinel\dashboard\radar_cal.html <user>@<keep-ip>:~/Presence/sentinel/dashboard/radar_cal.html
scp sentinel\dashboard\service.py <user>@<keep-ip>:~/Presence/sentinel/dashboard/service.py

# Fusion
scp sentinel\fusion\service.py <user>@<keep-ip>:~/Presence/sentinel/fusion/service.py
scp sentinel\fusion\identity_ledger.py <user>@<keep-ip>:~/Presence/sentinel/fusion/identity_ledger.py
scp sentinel\fusion\correlation.py <user>@<keep-ip>:~/Presence/sentinel/fusion/correlation.py
scp sentinel\fusion\validation.py <user>@<keep-ip>:~/Presence/sentinel/fusion/validation.py

# Brain
scp sentinel\brain\service.py <user>@<keep-ip>:~/Presence/sentinel/brain/service.py
scp sentinel\brain\narrative.py <user>@<keep-ip>:~/Presence/sentinel/brain/narrative.py

# Intelligence
scp sentinel\intelligence\emrf_intelligence.py <user>@<keep-ip>:~/Presence/sentinel/intelligence/emrf_intelligence.py

# Node adapter
scp sentinel\adapters\node_adapter.py <user>@<keep-ip>:~/Presence/sentinel/adapters/node_adapter.py

# Config
scp sentinel_config.json <user>@<keep-ip>:~/Presence/sentinel_config.json
```

## SCP to <broker-host> (Pi)

```powershell
scp sentinel\adapters\camera_adapter.py <user>@<broker-ip>:~/Presence/sentinel/adapters/camera_adapter.py
```

## Restart Services

### <keep-host> (Jetson)
```bash
ssh <user>@<keep-ip>
sudo systemctl restart sentinel-dashboard
sudo systemctl restart sentinel-fusion
sudo systemctl restart sentinel-brain
sudo systemctl restart sentinel-node-adapter
```

### <broker-host> (Pi)
```bash
ssh <user>@<broker-ip>
sudo systemctl restart sentinel-camera-adapter
```

## Tail Logs

```bash
# On <keep-host>
journalctl -u sentinel-dashboard -f
journalctl -u sentinel-fusion -f
journalctl -u sentinel-brain -f
journalctl -u sentinel-node-adapter -f

# On <broker-host>
journalctl -u sentinel-camera-adapter -f
```

## Quick Verify (MQTT)

```bash
mosquitto_sub -h <broker-ip> -t "sentinel/sensors/#" -v -C 20
mosquitto_sub -h <broker-ip> -t "sentinel/context/#" -v -C 10
mosquitto_sub -h <broker-ip> -t "sentinel/events/#" -v -C 5
```

## Dashboard

- URL: `http://<keep-ip>:8080`
- Layout: 50/50 split — floorplan left, sensor panels right (scrollable)
- Panel order (right column): EMRF, Thermal, Camera, Acoustic, Environment, Radar, Node Health
- Narrative and Zone Occupancy panels are hidden (still in DOM, JS still routes data)

## Important Notes

- Windows paths use **backslashes**, remote paths use **forward slashes**
- Do NOT use `~` on the local side — doesn't expand on Windows
- rsync does NOT work from Windows — always use scp
- Dashboard HTML is cached in memory — **must restart sentinel-dashboard after SCP**
- Always restart services after deploying new Python files
- the build agent (VM) can edit files on <build-host> via mount but **cannot SCP** (no LAN access from sandbox)

*Last updated: March 26, 2026*
