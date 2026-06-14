# Chat 32 — Session Changelog
**Date:** 2026-03-19 ~16:00–17:00 EDT
**Focus:** Fix EMRF device identity pipeline (P0 from Chat 31)

## Problem

EMRF (ESP32 BLE/WiFi scanner) was running but the `devices` topic never published to MQTT. Two compounding issues:

1. **JSON serialization buffer overflow** — The firmware's `publishMqttJson()` used a 3072-byte buffer, but 20 BLE + 20 WiFi devices with names could produce ~3300 bytes. When serialization exceeded the buffer, the publish silently failed.

2. **MAC address randomization** — Modern iOS/Android phones use randomized MACs for BLE advertisements and WiFi probe requests. The configured static MACs would never match the randomized MACs seen by the ESP32's passive scanner, making MAC-based identity matching impossible.

## Changes Made

### 1. Firmware: JSON Buffer Fix
**File:** `sentinel_node/network.h`
**Change:** `publishMqttJson()` buffer increased from 3072 → 4096 bytes
**Why:** Matches the PubSubClient MQTT buffer size (already 4096). With MAX_REPORT=20 per type × ~80 bytes/entry = ~3200 bytes + overhead, the old 3072 buffer was too small. The firmware logged `[NET] WARN: JSON too large for devices` to Serial but this wasn't visible remotely.
**Risk:** Low — only increases stack usage by 1024 bytes. ESP32-S3 has 512KB SRAM, heap showed 143K free.
**Deploy:** Requires firmware reflash via OTA or USB.

### 2. Config: BLE Name Identity Scaffolding
**File:** `sentinel_config.json`
**Change:** Added `ble_name` device entries (currently empty) for all 6 known persons
**Why:** Prepares for BLE name-based identity matching. Once the `devices` topic starts publishing, we'll see actual BLE broadcast names in the scan data and populate these fields.
**Format:**
```json
{"ble_name": "Alice's iPhone", "label": "phone", "type": "ble"}
```
**Risk:** None — empty ble_name entries are skipped by the identity map builder.

### 3. Config Loader: BLE Name Identity Map
**File:** `sentinel/config.py`
**Change:** Added `build_ble_name_identity_map()` method
**What it does:** Builds a lowercase BLE device name → `{"person_id", "name", "label"}` lookup from config. Case-insensitive matching.
**Risk:** None — new method, no existing code affected. Returns empty dict when no ble_names configured.

### 4. EMRF Intelligence: BLE Name Matching
**File:** `sentinel/intelligence/emrf_intelligence.py`
**Changes:**
- `__init__()` accepts optional `ble_name_identity` dict (backward compatible — defaults to empty)
- New device creation: after MAC lookup fails, tries BLE name fallback matching
- Late-binding resolution: existing unidentified devices get re-checked when a BLE name appears in a later scan
- `_ble_name_resolved` dict tracks MAC→person_id for session-level caching
- Logs `BLE name match` and `Late BLE name match` events at INFO level

**Risk:** Low — BLE name matching is additive. MAC matching still takes priority. Empty ble_name_identity dict means zero overhead on existing path.

### 5. Node Adapter: Wire BLE Name Identity
**File:** `sentinel/adapters/node_adapter.py`
**Change:** Calls `config.build_ble_name_identity_map()` and passes result to `EmrfIntelligence` constructor. Logs count at startup.
**Risk:** None — additive wiring only.

## Testing Performed

- Config load: 6 MAC entries, 0 BLE name entries (empty strings correctly skipped), 33 infra entries
- EmrfIntelligence backward compat: works without ble_name_identity argument
- BLE name matching: simulated BLE device with name "Test Phone" matched config entry "test phone" (case-insensitive) → correctly identified as person
- Syntax check: all 3 modified Python files pass `ast.parse()`

## Deployment Steps

1. **Firmware (OTA):** Flash updated `network.h` to ESP32 node-01 — fixes `devices` topic publishing
2. **Services restart:**
   ```bash
   sudo systemctl restart sentinel-node-adapter   # picks up BLE name identity
   sudo systemctl restart sentinel-brain           # picks up narrative changes from Chat 31
   ```
3. **After `devices` topic starts publishing:** Monitor BLE scan for phone names:
   ```bash
   mosquitto_sub -t 'home/sentinel/node-01/devices' -C 1 | python3 -m json.tool
   ```
4. **Populate BLE names:** Update `sentinel_config.json` with actual BLE broadcast names seen in scan data, then restart node-adapter again.

## Files Modified

| File | Change |
|------|--------|
| `sentinel_node/network.h` | JSON buffer 3072→4096 |
| `sentinel_config.json` | Added empty ble_name entries for 6 persons |
| `sentinel/config.py` | Added `build_ble_name_identity_map()` |
| `sentinel/intelligence/emrf_intelligence.py` | BLE name matching + late-binding resolution |
| `sentinel/adapters/node_adapter.py` | Wire ble_name_identity to EmrfIntelligence |

## Next Steps (for Chat 33+)

1. Flash firmware OTA → verify `devices` topic publishes
2. Capture BLE scan → identify phone broadcast names → update config
3. Also try: check each phone's WiFi settings for private WiFi MAC → update config MAC entries (Option 4 from Chat 31)
4. P1: Startup ghost reaper (unknowns from first 5s)
5. P2: Restart services after config changes
