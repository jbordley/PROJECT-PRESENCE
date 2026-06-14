# Chat 33 — Session Changelog
**Date:** 2026-03-19 ~17:00–17:30 EDT
**Focus:** Firmware OTA, verify devices topic, MQTT architecture discovery

## Accomplishments

### 1. Firmware OTA Flashed
- Flashed updated firmware from <build-host> (PlatformIO on Windows, COM14 USB)
- Confirmed `network.h` on <build-host> already had the 4096 buffer fix
- <keep-host>'s copy of the repo had old `buf[512]` — repos are out of sync between machines

### 2. `devices` Topic Now Publishing
- Confirmed from <broker-host>: `home/sentinel/node-01/devices` is live
- Sample: `wifi_count=30, ble_count=54` — ESP32 scanning 84 devices
- `MAX_REPORT=20` per type caps the JSON payload to fit 4096-byte buffer
- No `[NET] WARN` errors on serial — publishes succeeding

### 3. Person Resolution Confirmed Working
- EMRF intelligence resolving known persons by MAC
- Sample: `Person 1: active in office ~1.4m away, 1 device, present 4m | Person 2: present in office ~4.6m away, 1 device, present 4m`

### 4. System Architecture Clarified
- **<build-host>** (Windows) — PlatformIO, firmware builds, this build session
- **<keep-host>** (Jetson, <jetson-ip>) — runs sentinel-node-adapter, sentinel-brain services
- **<broker-host>** (Raspberry Pi, <broker-ip>) — runs Mosquitto MQTT broker
- **ESP32 node-01** (<node-01-ip>) — sensor node, publishes to <broker-host> broker
- <keep-host> subscribes to <broker-host> broker (`host: <broker-ip>` in sentinel_config.json)
- <keep-host> also has a LOCAL Mosquitto instance — caused confusion when `mosquitto_sub` without `-h` hit localhost instead of Pi

### 5. Known Config: 5 Devices (Not 6)
- Config key is `known_devices` (not `known_persons`)
- 5 entries: known devices
- The "5 vs 6" discrepancy from Chat 32 was a false alarm — 5 is correct

## Issues Discovered

### MQTT Reconnection After Broker Restart
- Restarting Mosquitto on <broker-host> caused <keep-host>'s adapter to lose connection
- ESP32 serial reported `mqtt=OK` but wasn't actually publishing (stale state after broker restart)
- Power cycling ESP32 + broker restart fixed ESP32 side
- <keep-host>'s sentinel-node-adapter may need restart after broker restart
- **TODO:** Add MQTT reconnect resilience to both ESP32 firmware and Python adapter

### BLE Noise / False Alerts
- Randomized BLE MACs creating constant new_device/arrival/departure churn
- Devices appearing for 0-3 minutes then departing — BLE advertisement spam
- Threat engine flagging close unknowns as `threat=high` — false positives from own devices' randomized BLE MACs
- **TODO:** Add minimum presence threshold (e.g., 2+ scan cycles) before emitting new_device events

### Repo Sync
- <build-host> repo has correct firmware (`buf[4096]`)
- <keep-host> repo has old firmware (`buf[512]`)
- Config and Python files may also be out of sync
- **TODO:** Git push/pull to sync repos across machines

## Files Modified
None in this session — firmware was already correct on <build-host> from Chat 32.

## Next Steps (for Chat 34+)

1. **Restart sentinel-node-adapter on <keep-host>** — verify it reconnects to Pi broker and receives `devices` topic
2. **Capture BLE broadcast names** — from `devices` topic, identify phone names for BLE name matching
3. **BLE noise filter** — add minimum scan count before emitting new_device events
4. **Startup ghost reaper** — P1 from Chat 32 (unknowns from first 5s)
5. **Git sync** — push/pull repos between <build-host>, <keep-host>, <broker-host>
6. **MQTT reconnect resilience** — handle broker restarts gracefully
