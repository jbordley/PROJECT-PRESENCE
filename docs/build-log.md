# Build Log

## March 2026

### Phase 1 — Infrastructure (Chats 1-2)
- ✅ <broker-host> (Pi 4) flashed, SSH working
- ✅ Wake-on-LAN confirmed: Pi → PC (`XX:XX:XX:XX:XX:XX`)
- ✅ RuView Docker running on <keep-host> in simulate mode
- ✅ Mosquitto MQTT broker installed and network-accessible
- ✅ Cross-machine MQTT confirmed: <broker-host> → <keep-host>

### Architecture Decision: RuView → Custom Firmware (Chat 2)
- ✅ RuView confirmed non-functional (fake CSI data, simulation only)
- ✅ Replaced with custom sentinel_node firmware (Arduino/PlatformIO)
- ✅ Added HLK-LD2450 24GHz per zone for multi-target tracking
- ✅ CSI pipeline retained as future research track

### Intelligence Architecture (Chat 5)
- ✅ World model + causal reasoning philosophy defined
- ✅ Context / Intent / Specification three-pillar architecture
- ✅ MQTT topic hierarchy designed (`home/sentinel/{node_id}/...`)
- ✅ Three-stage data flow: perception → narrative → action
- ✅ Occupancy state machine defined
- ✅ sentinel_platform_spec.md Section 6.4 written

### Sentinel Node Firmware (Chats 6-15)
- ✅ sentinel_node.ino — main firmware with modular sensor architecture
- ✅ config.h — node identity, WiFi, pin assignments, timing, sensor enables
- ✅ network.h — WiFi STA (hidden SSID), MQTT with LWT, UDP, OTA, mDNS
- ✅ ld2450.h — binary UART parser for LD2450 (3 targets, X/Y/speed)
- ✅ device_scanner.h — BLE/WiFi passive scanning with mutex-protected device table
- ✅ acoustic.h — SPH0645 I2S microphone, RMS/peak/impulsive detection
- ✅ bme688.h — BME688 environmental sensor (temp, humidity, pressure, VOC)
- ✅ LD2450 serial buffer bug fixed (setRxBufferSize before begin)
- ✅ BLE/WiFi preprocessor guards added (stacks don't link when disabled)
- ✅ NULL mutex crash fixed (scanner methods called without begin())
- ✅ **Node-01 stable** — WiFi + MQTT + acoustic running, ~250KB heap free

### YDLidar X4 Pro Integration (Chat 16)
- ✅ ydlidar.h — full protocol parser (4-state machine, 360° scan assembly, XOR checksum)
- ✅ config.h — lidar constants (128000 baud, PWM motor control, 12-zone MQTT output)
- ✅ sentinel_node.ino — lidar wired in under `#if ENABLE_LIDAR` guards
- ⏳ ENABLE_LIDAR remains `false` — waiting on powered USB hub for testing

### BLE/WiFi Scanner Boot Fix (Chat 17)
- 🐛 **BUG**: BLE/WiFi scanner caused boot loop (first reported Chat 14, disabled as workaround)
- 🔍 **DIAGNOSIS**: Staged re-enable — WiFi scan alone booted clean, confirming BLE stack was the culprit
- ✅ **FIX**: Added defensive guards to `device_scanner.h begin()`:
  - Mutex creation failure check (bail early instead of crash)
  - `esp_wifi_set_promiscuous()` return code check
  - `BLEDevice::getScan()` null check
  - Serial breadcrumbs at each init stage for future debugging
- ✅ **RESULT**: Both BLE + WiFi scan now boot clean and run stable
  - WiFi only: ~249KB heap free
  - WiFi + BLE: ~143KB heap free (~100KB for BLE stack)
  - 39 BLE devices detected within 20s

### Hub-Side Node Adapter (Chat 18)
- ✅ `sentinel/adapters/node_adapter.py` (~340 lines) — bridges `home/sentinel/{node_id}/*` → `sentinel/sensors/{zone}/*/raw`
- ✅ Sensor mapping: radar, devices→emrf, environment→voc, acoustic, lidar, status→health
- ✅ Environment enrichment — BME688 context injected into radar/acoustic at adapter level
- ✅ `sentinel/adapters/__main__.py` updated — runs both CSI + Node adapters
- ✅ `sentinel/config.py` — node_1 sensors updated, node 2-4 templates stubbed
- ✅ `pyproject.toml` added (setuptools build backend)
- ✅ **Deployed live on <keep-host>** via scp — node adapter connected to MQTT at <jetson-ip>
- ✅ **Confirmed live data**: radar (2 targets, 0.9 confidence), VOC (24°C, 37.8% humidity), emrf (30s intervals)
- ✅ All publishing to `sentinel/sensors/office/*/raw`

### Current Node-01 Status (as of Chat 18)
```
WiFi:     Connected to "YOUR_SSID" (hidden) — <jetson-ip>, RSSI ~-51
MQTT:     Connected to <jetson-ip>
Radar:    Running (LD2450, 2 targets tracked, 0.9 confidence, ~1Hz)
BLE:      Running (passive scan, 20s intervals, 128-slot table)
WiFi Scan: Running (promiscuous probe sniffing)
BME688:   Running (24°C, 37.8% humidity, 73K gas ohms, ~5.5s)
Acoustic: Running (I2S 16kHz — not physically wired yet, reads 0 dB)
Thermal:  Running
Lidar:    Disabled (waiting on powered USB hub)
Heap:     ~140-150KB free (stable with all sensors)
COM port: COM14
```

### Hub-Side Services Status (as of Chat 18)
```
Node Adapter:  ✅ Live on <keep-host> — bridging node-01 → office zone
CSI Adapter:   Built, not yet running (CSI is future research track)
Fusion Service: Not yet started
Brain Service:  Not yet started
```

### Dashboard Live + EMRF Fix (Chats 20-21)
- ✅ Dashboard deployed at http://<jetson-ip>:8080
- ✅ Radar panel live (2 targets, blue dots, 0.9 confidence)
- ✅ VOC panel live (22.8°C, 38.5% humidity, 67K gas)
- ✅ Acoustic panel live
- ✅ Node health panel live
- 🐛 **BUG**: EMRF panel not populating — ESP32 publishes `devices` but adapter drops message
- 🔍 **ROOT CAUSE**: Double buffer overflow in firmware
  - `publishMqttJson()` serializes to 512-byte buffer — device JSON with 20+ devices easily 1500+ bytes → truncated
  - PubSubClient MQTT buffer was 1024 bytes — even if serialization worked, MQTT would truncate
  - Node adapter `json.loads()` fails on truncated JSON, silently returns
- ✅ **FIX (adapter side — immediate, no firmware flash)**:
  - Added `_salvage_json()` method to `node_adapter.py` — strips trailing garbage, attempts to close truncated JSON, falls back to regex extraction of counts
  - Logs salvaged vs unparseable payloads for debugging
- ✅ **FIX (firmware side — ready for next OTA flash)**:
  - `network.h`: MQTT buffer 1024→4096, serialization buffer 512→3072, overflow warning log
  - `device_scanner.h`: `toJson()` now reports top 20 devices per type by RSSI (strongest first), total counts still reflect all active devices
- ✅ Deployment scripts: `scripts/deploy_chat21.sh` (full systemd deploy), `scripts/test_emrf_fix.sh` (quick adapter test)
- ✅ Systemd unit files ready: node-adapter, fusion, dashboard (all 3 services)

### Hub-Side Services Status (as of Chat 21)
```
Node Adapter:  ✅ Updated with EMRF salvage fix — ready to deploy
Fusion Service: ✅ Built — ready to start via systemd
Dashboard:      ✅ Live at :8080 — ready for systemd management
CSI Adapter:    Built, not yet running (CSI is future research track)
Brain Service:  Not yet started
```

### Systemd + Camera Adapter Fixes (Chats 22-24)

#### Camera Adapter systemd Fix (Chat 23)
- 🐛 **BUG**: `sentinel-camera-adapter.service` failing with exit code 226/NAMESPACE
- 🔍 **ROOT CAUSE**: `ProtectSystem=strict`, `PrivateTmp=true`, and `ReadWritePaths=` directives prevent video device access
- ✅ **FIX**: Commented out `ProtectSystem=strict`, `PrivateTmp=true`, and `ReadWritePaths=` in the unit file

#### Port Race Condition (Chats 23-24)
- 🐛 **BUG**: Camera adapter crash-looping on port 8089; dashboard crash-looping on port 8080 (204+ restarts)
- 🔍 **ROOT CAUSE**: On systemd restart, the previous process's port binding lingers. New process starts, gets `[Errno 98] address already in use`, exits, systemd restarts, repeat
- ✅ **FIX (manual)**: `systemctl stop` → `fuser -k <port>/tcp` → `systemctl start`
- ✅ **FIX (permanent)**: Add to both systemd unit files:
  ```ini
  ExecStartPre=/bin/sleep 2
  ExecStartPre=-/usr/bin/fuser -k -s <port>/tcp
  ```

#### TC001 YUYV Frame Fix (Chat 23)
- 🐛 **BUG**: TC001 thermal camera returning 3-channel YUYV frames, causing OpenCV color conversion errors
- ✅ **FIX**: Added `if frame.shape[2] == 2: frame = frame[:, :, 0]` after `_crop_frame()` at line ~358 in `camera_adapter.py`
- ✅ Both cameras operational: Arducam 640x480 @1fps, TC001 644x384 crop-left @2fps

#### Thermal Temperature Scaling Bug (Chat 24 — identified, fix pending)
- 🐛 **BUG**: Dashboard shows Max Temp 255.0°C, Min 0.0°C, Mean 111.9°C — raw byte values displayed as Celsius
- 🔍 **ROOT CAUSE**: After YUYV fix, frame is single-channel 0-255 (luminance). `raw.max()` = 255 which is < 1000, so the centi-Kelvin branch is skipped and values are treated as "already Celsius"
- ✅ **FIX**: Added 8-bit luminance scaling branch: if max ≤ 255, map to 15-45°C indoor range (same as the BGR→gray path)

#### Dashboard Snapshot URL Bug (Chat 24 — identified, fix pending)
- 🐛 **BUG**: Thermal and camera panels show black/empty — images not loading
- 🔍 **ROOT CAUSE**: Dashboard JS uses `location.hostname` (<jetson-ip> = <keep-host>) as snapshot host, but camera adapter snapshots are served from <broker-host> (<jetson-ip>:8089). Images fetch from wrong host.
- ✅ **FIX**: Hardcode `SNAPSHOT_HOST` to <broker-host> IP since camera adapter always runs there

### Hub-Side Services Status (as of Chat 24)
```
Node Adapter:    ✅ Live on <keep-host> — bridging node-01 → office zone
Camera Adapter:  ✅ Live on <broker-host> — Arducam + TC001, snapshots on :8089
Fusion Service:  ✅ Live on <keep-host>
Brain Service:   ✅ Live on <keep-host> — narrative generating
Dashboard:       ✅ Live on <keep-host> at :8080 — all panels rendering
CSI Adapter:     Built, not yet running (CSI is future research track)
```

### TC001 Y16 + Thermal Hardening (Chat 25)

#### Frame Tearing Fix
- 🐛 **BUG**: Occasional torn/corrupted thermal frames
- 🔍 **ROOT CAUSE**: `read()` can retrieve a frame mid-scanout from the USB UVC device
- ✅ **FIX**: Replaced `read()` with `grab()+retrieve()` — grab latches the frame buffer atomically, retrieve decodes it afterward. Also applied to warm-up loop for consistency.

#### Y16 Radiometric Mode — Two-Path Approach
- 🐛 **BUG**: TC001 falling back to YUYV luminance mapping (estimated 15-45°C) instead of using Y16 centi-Kelvin radiometric data
- 🔍 **ROOT CAUSE**: `v4l2-ctl --list-formats-ext` shows TC001 only advertises YUYV — no Y16 FOURCC. However, the device lists oddball resolutions (4x12305, 4x12621, 8x12578) that are suspiciously close to 256×192×2 bytes (a Y16 frame packed as fake YUYV).
- ✅ **FIX (Path A)**: Request native 256x192 YUYV — pure thermal, no composite, skip crop. Cleaner luminance mapping.
- ✅ **FIX (Path B)**: At startup, probe oddball resolutions. Capture a test frame, flatten to bytes, reinterpret as uint16, check if values are in centi-Kelvin range (29000-31500 for room temp). If yes, use `y16_raw` mode for real radiometric data.
- Startup order: Path B first (best data), fall back to Path A (native YUYV), fall back to composite crop (existing behavior).
- ⏳ **VERIFY**: Deploy to <broker-host> and check logs for "Y16 RAW MODE CONFIRMED" or native 256x192 fallback.

#### False Positive Reduction
- 🐛 **BUG**: 52 false positive human blobs per frame from furniture, walls, ambient heat
- ✅ **FIX**: Tightened thresholds — temp range 28-42°C → 30-40°C, min blob area 50px → 200px
- ✅ **RESULT**: False positives eliminated, real human blobs still detected

#### Firmware Buffer Fix (OTA flashed — Chat 26)
- ✅ Source updated: `network.h` MQTT buffer 1024→4096, serialize buffer 512→3072
- ✅ Source updated: `device_scanner.h` top-20 device limit by RSSI
- ✅ **OTA flash completed** (Chat 26): `pio run -t upload --upload-port sentinel-node-01.local`
- EMRF panel should now show clean JSON without adapter salvage workaround

### Hub-Side Services Status (as of Chat 25)
```
Node Adapter:    ✅ Live on <keep-host> — bridging node-01 → office zone
Camera Adapter:  ✅ Updated — Y16 at init, grab()+retrieve(), tightened thresholds
Fusion Service:  ✅ Live on <keep-host>
Brain Service:   ✅ Live on <keep-host> — narrative generating
Dashboard:       ✅ Live on <keep-host> at :8080 — all panels rendering
CSI Adapter:     Built, not yet running (CSI is future research track)
```

### TC001 Y16 Confirmed + USB Re-enumeration (Chat 26)

#### Y16 Raw Extraction — Confirmed Working
- ✅ **Y16 raw mode verified**: 4×12621 oddball resolution produces 2664-byte header + 256×192 uint16 thermal data
- ✅ Data biased at `0x8000` (32768) with AGC-scaled ~8-bit range (32768-33023), mapped to 15-45°C
- ✅ `_find_thermal_device()` auto-detect scans video0-9, probes oddball resolutions via `_try_y16_raw_extraction()`
- ✅ Startup order: Y16 raw probe first → native 256×192 YUYV fallback → composite crop fallback

#### USB Re-enumeration Bug
- 🐛 **BUG**: After unplugging/replugging TC001, it re-enumerated as `/dev/video2` (+ `/dev/video3`) instead of `/dev/video1`
- 🔍 **ROOT CAUSE**: Linux USB device enumeration is non-deterministic — device index depends on plug order and kernel assignment
- ✅ **FIX**: Auto-detect already handles this — `_find_thermal_device()` scans video0-9 and `_try_y16_raw_extraction()` probes each candidate. Service restart was sufficient to pick up the new device index.
- ℹ️ **WORKAROUND (if auto-detect fails)**: `--thermal-device 2` CLI flag forces specific device index

#### Pi Undervoltage Warning
- ⚠️ **ISSUE**: Raspberry Pi showing undervoltage detected warnings after TC001 replug
- 🔍 **ROOT CAUSE**: TC001 draws significant USB power, causing brownout flicker on the Pi's power rail
- 💡 **RECOMMENDATION**: Use a powered USB hub for the TC001, or upgrade to a 5V/3.5A+ Pi power supply
- ⏳ **STATUS**: Monitoring — system still operational but may cause intermittent USB disconnects under load

#### Known Device Identity Registry (Chat 26)
- ✅ `sentinel_config.json` — added `known_devices` section: per-person MAC→identity mapping with device labels and types
- ✅ `sentinel/config.py` — added `known_devices` field to `SentinelConfig`, `build_mac_identity_map()` helper builds MAC→identity lookup
- ✅ `sentinel/adapters/node_adapter.py` — `_tag_devices_with_identity()` annotates each device with person_id/name/label; `_count_known_by_person()` produces per-person counts; known devices boost EMRF confidence (+0.1 per device, max +0.25)
- ✅ `sentinel/dashboard/index.html` — EMRF panel now shows per-person breakdown with device labels (e.g. "Person 1: 3 — phone, laptop, watch")
- ⏳ **NEEDS**: Actual MAC addresses populated in `sentinel_config.json` (placeholder XX:XX values currently)

### Hub-Side Services Status (as of Chat 26)
```
Node Adapter:    ✅ Live on <keep-host> — bridging node-01 → office zone
Camera Adapter:  ✅ Live on <broker-host> — Y16 raw mode confirmed, auto-detect working
Fusion Service:  ✅ Live on <keep-host>
Brain Service:   ✅ Live on <keep-host> — narrative generating
Dashboard:       ✅ Live on <keep-host> at :8080 — all panels rendering
CSI Adapter:     Built, not yet running (CSI is future research track)
```

### Known Device Identity — Real MACs + Deployment (Chat 27)

#### MAC Address Population
- ✅ Router DHCP client table captured — full network inventory
- ✅ `sentinel_config.json` populated with device MACs (example placeholders):
  - Device 1 (XX:XX:XX:XX:XX:XX)
  - Device 2 (XX:XX:XX:XX:XX:XX)
  - Device 3 (XX:XX:XX:XX:XX:XX)
  - Device 4 (XX:XX:XX:XX:XX:XX)
  - Device 5 (XX:XX:XX:XX:XX:XX)
- ✅ `build_mac_identity_map()` verified returning all 5 entries locally

#### Deployment to <keep-host> — Errors & Fixes

##### Stale Process / MQTT Reconnect Loop
- 🐛 **BUG**: Node adapter reconnecting to MQTT every 2 seconds — dashboard showed no identity data
- 🔍 **ROOT CAUSE**: A manually-launched adapter from March 16 (PID 65181, `python3 -m sentinel.adapters --mqtt-host <broker-ip> --log-level DEBUG`) was still running alongside the systemd service. Both used the same MQTT client ID (`sentinel-node-adapter`), causing the broker to bounce them back and forth.
- ✅ **FIX**: `kill 65181` to remove the stale manual process, then `systemctl restart sentinel-node-adapter` — clean single connection established

##### Missing `Loaded 5 known device MACs` Log Line
- 🐛 **BUG**: After restart, adapter connected cleanly but no MAC loading log appeared, identity tagging not working
- 🔍 **ROOT CAUSE**: The scp placed the updated `sentinel_config.json` at `~/sentinel/sentinel_config.json` (correct), but the Python source files (`~/Presence/sentinel/`) still had pre-Chat-26 code without `_tag_devices_with_identity()`, `_count_known_by_person()`, or `build_mac_identity_map()`
- ✅ **FIX**: scp'd updated `node_adapter.py` and `config.py` to `~/Presence/sentinel/`

##### AttributeError on `build_mac_identity_map`
- 🐛 **BUG**: After syncing `node_adapter.py` but forgetting `config.py`, adapter crashed with `AttributeError: 'SentinelConfig' object has no attribute 'build_mac_identity_map'`
- 🔍 **ROOT CAUSE**: `node_adapter.py` called `config.build_mac_identity_map()` but the old `config.py` on <keep-host> didn't have that method
- ✅ **FIX**: scp'd `config.py` to <keep-host>, restarted — `Loaded 5 known device MACs` confirmed in logs

#### Live Identity Tagging Confirmed
- ✅ `mosquitto_sub` on EMRF topic shows `known_count: 2`, `persons: {"person1": {"name": "Person 1", "count": 1, "devices": ["phone"]}}` — identity tagging live
- ✅ Device 1 seen at RSSI -38 (very close), Device 2 at RSSI -52
- ✅ BLE scan also picking up named devices (e.g. BLE advertisements with personal identifiers)
- ✅ Dashboard EMRF panel showing Known/Unknown counts

#### Key Lesson: Source Sync
- ⚠️ **LESSON**: When deploying code changes to <keep-host>, must sync ALL modified Python files — not just config JSON. The adapter, config module, and dashboard HTML all need to match. Consider a deploy script or git pull workflow.

### Hub-Side Services Status (as of Chat 27)
```
Node Adapter:    ✅ Live on <keep-host> — identity tagging active, 5 known MACs loaded
Camera Adapter:  ✅ Live on <broker-host> — Y16 raw mode confirmed, auto-detect working
Fusion Service:  ✅ Live on <keep-host>
Brain Service:   ✅ Live on <keep-host> — narrative generating
Dashboard:       ✅ Live on <keep-host> at :8080 — EMRF showing per-person breakdown
CSI Adapter:     Built, not yet running (CSI is future research track)
```

### EMRF Intelligence Engine Live (Chat 28)
- ✅ `sentinel/intelligence/__init__.py` + `emrf_intelligence.py` — new intelligence module
- ✅ Transforms raw WiFi/BLE scans → per-person presence with device labels, RSSI smoothing, distance estimation, proximity bands
- ✅ Session tracking: arrival/departure events, duration, settled/transient status
- ✅ Zone confidence scoring (0.0–0.95) with person boost and proximity boost
- ✅ Three-tier classification: known persons → infrastructure → truly unknown
- ✅ Events on `sentinel/events/{zone}/emrf/{arrival|departure|new_device}`
- ✅ Node adapter updated to use intelligence engine
- ✅ Dashboard HTML updated with enriched EMRF panel
- ✅ Confirmed live: `EmrfIntelligence initialized with 5 known person MACs (sanitized), 33 infra MACs`
- ✅ `mosquitto_sub` output shows full enriched payload: persons with proximity, confidence, sessions

### Device Count Consistency Fix (Chat 29)
- 🐛 **BUG**: `total_devices` (raw WiFi+BLE count) didn't match `known_count + infra_count + unknown_count` (deduplicated tracked count)
- 🔍 **ROOT CAUSE**: `total_devices` used `len(wifi_devices) + len(ble_devices)` (raw pre-dedup), while category counts came from `present_devices` (deduplicated, includes timeout-window devices from previous scans)
- ✅ **FIX**: `total_devices` now equals `total_present` (deduplicated tracked count). Added `raw_scan_total` field for debugging.
- ✅ **VERIFIED**: `match: True` — `known(1) + infra(1) + unknown(51) = 53 = total_devices`
- ⚠️ `.pyc` cache required clearing before new code loaded: `find ~/Presence -name "*.pyc" -delete && find ~/Presence -name "__pycache__" -type d -exec rm -rf {} +`

### Sensor Fusion Inference Engine — Design (Chat 29)
- ✅ Architecture designed: four-layer inference engine
  - **Layer 1 — Assertions**: Each sensor publishes typed claims (e.g., thermal: "2 heat sources at X,Y")
  - **Layer 2 — Correlation**: Match assertions across sensors (thermal blob → EMRF MAC → camera face)
  - **Layer 3 — Inference**: Reasoning rules on correlated data (thermal+IR present, radar silent → stationary)
  - **Layer 4 — Narrative**: Human-readable conclusions with confidence and reasoning chain
- ✅ Key insight: sensor disagreements are information, not errors (radar seeing 1 when thermal sees 2 → one person is still)
- ✅ Inference rules drafted:
  - Thermal + IR present, radar silent → stationary person
  - Acoustic typing > radar motion → seated typing activity
  - EMRF device, no thermal → device left behind
  - Thermal present, no EMRF → unknown person (intruder path)
  - Radar spike + acoustic anomaly, no EMRF → pet or physical event
- ⏳ Implementation next: assertion schema → correlation engine → inference rules → narrative output

### Hub-Side Services Status (as of Chat 29)
```
Node Adapter:    ✅ Live on <keep-host> — identity tagging + EMRF intelligence engine active
Camera Adapter:  ✅ Live on <broker-host> — Y16 raw mode confirmed, auto-detect working
Fusion Service:  ✅ Live on <keep-host> — three-layer validation
Brain Service:   ✅ Live on <keep-host> — narrative generating
Dashboard:       ✅ Live on <keep-host> at :8080 — all panels rendering
Intelligence:    ✅ EMRF engine live — person presence, proximity, sessions, events
CSI Adapter:     Built, not yet running (CSI is future research track)
Inference Engine: ⏳ Designed — cross-sensor conclusion layer (build next)
```

### BLE MAC Randomization Filter + Multi-Chat Fixes (Chats 30-33)
- ✅ BLE address type fix: `getType()` → bit-check for filtering randomized MACs
- ✅ Firmware JSON buffer increased: 3072→4096
- ✅ BLE name identity scaffolding added
- ✅ EMRF intelligence updates
- ✅ Person resolution fixes
- ✅ Commit `aeb8472`: 29 files changed, 4,317 insertions

### Repo Sync, Firmware Flash & Deployment (Chat 34)

#### Git Push + <keep-host> Sync
- ✅ Commit `aeb8472` pushed from <build-host> (build agent lacks GitHub credentials)
- ✅ <keep-host> synced via SCP (git auth broken on <keep-host> — PAT/SSH key needed)
- ✅ Correct paths confirmed: <keep-host> = `/home/user/Presence/`
- ✅ `sentinel-node-adapter` restarted and receiving EMRF events from office

#### <broker-host> Sync
- ✅ SCP'd `sentinel/`, `sentinel_node/`, and `sentinel_config.json` to <broker-host>
- ✅ Config copied to `/home/user/sentinel/` (where camera adapter expected it)
- ✅ `sentinel-camera-adapter` restarted — Arducam + TC001 Y16 raw mode confirmed
- ✅ <broker-host> runs: `sentinel-camera-adapter.service` (camera adapter only)

#### Firmware Flash
- ✅ Flashed firmware with `addr_type` bit-check fix for BLE MAC randomization filtering

#### Config Path Fix (commit `53a9299`)
- 🐛 **BUG**: `config.py` hardcoded config path to `~/sentinel/sentinel_config.json` — doesn't exist on fresh deployments, config file had to be manually copied outside the repo
- 🔍 **ROOT CAUSE**: `CONFIG_PATH` default was `Path.home() / "sentinel" / "sentinel_config.json"` instead of relative to repo
- ✅ **FIX**: Changed to `Path(__file__).resolve().parent.parent / "sentinel_config.json"` — resolves relative to the `sentinel/` package, so config is found at repo root on any machine
- ✅ `SENTINEL_CONFIG` env var still works as override
- ✅ Deployed to both <keep-host> and <broker-host>, services restarted

### Hub-Side Services Status (as of Chat 34)
```
Node Adapter:    ✅ Live on <keep-host> — identity tagging + EMRF intelligence active
Camera Adapter:  ✅ Live on <broker-host> — Y16 raw mode, config path fix deployed
Fusion Service:  ✅ Live on <keep-host> — three-layer validation
Brain Service:   ✅ Live on <keep-host> — narrative generating
Dashboard:       ✅ Live on <keep-host> at :8080 — all panels rendering
Intelligence:    ✅ EMRF engine live — person presence, proximity, sessions, events
CSI Adapter:     Built, not yet running (CSI is future research track)
Inference Engine: ⏳ Designed — cross-sensor conclusion layer (build next)
```

---

## Errors & Fixes Log

| Chat | Issue | Root Cause | Fix | Status |
|------|-------|------------|-----|--------|
| 14 | Boot loop with BLE/WiFi scanner enabled | BLE stack init (~100KB alloc) crashing during setup | Disabled both as workaround | Resolved in Chat 17 |
| 15 | NULL mutex crash in scanner | `scanner.deviceCount()` called in heartbeat before `scanner.begin()` | Runtime flag guards in heartbeat section | ✅ Fixed |
| ~10 | LD2450 serial buffer overflow | Default 256-byte RX buffer too small for 256000 baud | `setRxBufferSize(1024)` before `Serial2.begin()` | ✅ Fixed |
| 17 | BLE/WiFi boot loop (full fix) | BLE stack init timing/memory | Defensive guards + staged re-enable confirmed BLE safe | ✅ Fixed |
| 20-21 | EMRF panel not populating | Firmware 512B serialize + 1024B MQTT buffer overflow with 20+ BLE devices | Adapter: salvage truncated JSON. Firmware: 4096B MQTT, 3072B serialize, top-20 device limit | ✅ Fixed (OTA flashed Chat 26) |
| 23 | Camera adapter 226/NAMESPACE | systemd `ProtectSystem=strict` blocks video device access | Commented out ProtectSystem, PrivateTmp, ReadWritePaths | ✅ Fixed |
| 23-24 | Port 8089/8080 crash-loop | Orphan port bindings persist across systemd restarts | `ExecStartPre=-/usr/bin/fuser -k -s <port>/tcp` | ✅ Fixed |
| 23 | TC001 YUYV 2-channel frame | OpenCV gets 2-channel YUYV instead of BGR/grayscale | Extract channel 0: `frame[:, :, 0]` after crop | ✅ Fixed |
| 24 | Thermal temps show 0-255°C | 8-bit luminance treated as Celsius (max 255 < 1000 threshold) | Add 8-bit scaling branch: map 0-255 → 15-45°C | ✅ Fixed |
| 24 | Dashboard snapshots black | JS uses `location.hostname` (<keep-host>) but snapshots served from <broker-host> | Hardcode SNAPSHOT_HOST to <broker-host> IP | ✅ Fixed |
| 25 | Frame tearing on TC001 | `read()` retrieves mid-scanout frames | `grab()+retrieve()` for atomic frame capture | ✅ Fixed |
| 25 | TC001 stuck on YUYV luminance | V4L2 driver only advertises YUYV; Y16 may be hidden in oddball resolutions | Path B: probe oddball res for raw Y16; Path A: native 256x192 YUYV | ✅ Fixed (deploy to verify) |
| 25 | 52 false positive thermal blobs | Thresholds too loose (28-42°C, 50px min) | Tightened to 30-40°C, 200px min | ✅ Fixed |
| 26 | TC001 re-enumerated as /dev/video2 | USB re-enumeration after replug assigns new device index | Auto-detect scans video0-9; service restart picks up new index. `--thermal-device N` as manual override | ✅ Fixed |
| 26 | Pi undervoltage warnings | TC001 USB power draw causes brownout on Pi power rail | Powered USB hub or 5V/3.5A+ PSU recommended | ⚠️ Monitoring |
| 27 | MQTT reconnect loop every 2s | Stale manual adapter process (PID 65181 from Mar 16) fighting systemd service for same client ID | `kill 65181` then restart service | ✅ Fixed |
| 27 | No identity tagging on <keep-host> | `node_adapter.py` and `config.py` on <keep-host> were pre-Chat-26 code | scp both files from <build-host>, restart | ✅ Fixed |
| 27 | `AttributeError: build_mac_identity_map` | Synced `node_adapter.py` but forgot `config.py` — method didn't exist in old config | scp `config.py` to <keep-host> | ✅ Fixed |
| 29 | `total_devices` ≠ `known + infra + unknown` | `total_devices` was raw pre-dedup count, category counts from deduplicated tracked set | `total_devices = total_present`, added `raw_scan_total` | ✅ Fixed |
| 29 | New code not loading after SCP + restart | `.pyc` cache serving old bytecode | `find -name "*.pyc" -delete` + clear `__pycache__` dirs | ✅ Fixed |
| 34 | Config "using defaults" on fresh deploys | `CONFIG_PATH` hardcoded to `~/sentinel/sentinel_config.json` — doesn't exist on <broker-host> | `Path(__file__).resolve().parent.parent / "sentinel_config.json"` — resolves relative to repo | ✅ Fixed |
| 34 | Duplicate `sentinel_config.json` on <keep-host> | Config at 3 locations: `~/Presence/`, `~/sentinel/`, `~/Presence/sentinel/` | Config path fix eliminates need for `~/sentinel/` copy | ✅ Fixed |

---

## Pending

### Immediate
- [x] Deploy `camera_adapter.py` to <broker-host> and restart service
- [x] Verify Y16 raw extraction working (4×12621 oddball res, uint16 thermal data)
- [x] Verify auto-detect finds TC001 after USB re-enumeration
- [x] OTA flash node-01 firmware: `pio run -t upload --upload-port sentinel-node-01.local`
- [x] Verify EMRF panel shows clean JSON after OTA flash — ✅ confirmed Chat 27
- [x] Fix node adapter reconnect loop — ✅ killed stale process, Chat 27
- [x] Populate device MAC addresses in `sentinel_config.json` — ✅ 5 devices, Chat 27
- [x] Deploy identity tagging code to <keep-host> — ✅ node_adapter.py + config.py synced, Chat 27
- [ ] Add `known_infrastructure` registry to `sentinel_config.json` — three-tier device classification (People / Infrastructure / Truly Unknown)
- [ ] Update adapter + dashboard for three-tier breakdown
- [x] Sync source to <broker-host> — SCP'd sentinel/, sentinel_node/, sentinel_config.json (Chat 34)
- [x] Config path fix — `config.py` now resolves relative to repo root (Chat 34, commit `53a9299`)
- [ ] Add `ExecStartPre` fuser lines to both systemd units on <broker-host> and <keep-host>
- [ ] Fix <keep-host> git credentials — set up PAT or SSH key so future syncs don't require SCP
- [ ] Clean up stale `~/sentinel/sentinel_config.json` copies on <keep-host> and <broker-host>
- [ ] Set up proper deploy workflow (git pull or deploy script) to avoid source sync issues

### Hardware (Priority)
- [ ] Powered USB hub for TC001 on <broker-host> (resolve undervoltage warnings)

### Hardware
- [ ] Wire acoustic sensor on Node-01
- [ ] Flash remaining 3 ESP32-S3 nodes
- [ ] Test YDLidar X4 Pro with powered USB hub → flip ENABLE_LIDAR = true
- [ ] Order MR60BHA2 desk vitals sensor
- [ ] SSH key auth setup (<broker-host> ↔ <keep-host> ↔ <build-host>)

### Validation (after lidar enabled)
- [ ] Validate lidar protocol (checksums, scan counts, point counts)
- [ ] Verify MQTT lidar zone summaries on `home/sentinel/node-01/lidar`
- [ ] Tune lidar motor duty (target 6-12 Hz scan frequency)

### Sensor Fusion Inference Engine (next major milestone)
- [ ] Define assertion schema — standardize what each sensor "claims" per scan
- [ ] Build correlation layer — match assertions across sensors (thermal blob ↔ EMRF MAC ↔ camera face)
- [ ] Implement inference rules — stationary detection, activity typing, intruder path, device-left-behind
- [ ] Build narrative output — human-readable conclusions with confidence + reasoning chain
- [ ] Wire into brain service — replace raw sensor forwarding with conclusion-based narrative

### Future
- [ ] Hub-side lidar consumer — Python script for presence fusion
- [ ] Build CSI aggregator bridge script on Jetson (future)
- [ ] Wire n8n to MQTT presence events
- [ ] Identity mapping — wire face recognition to narrative (currently all "Unknown entity")
- [ ] Re-scp + `pip install -e .` on <keep-host> (pyproject.toml fix)
