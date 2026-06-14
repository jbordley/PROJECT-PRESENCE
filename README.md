# Project Presence

**A privacy-first world model of a physical space — who is present, where they are, and what's happening — built entirely on local hardware. No cloud.**
WiFi CSI sensing · mmWave radar · Dual cameras (visual + thermal) · Face recognition · Local AI · No cloud

---

## What it is

Project Presence turns raw multi-sensor data into a continuously updated **world model**: a living, structured picture of who is in a space, where they are, and what is normal. **Sentinel perceives** (sensors → fusion → world model); **Presence reasons** over it.

The world model is the product. It is the substrate a reasoning layer — or any downstream agent — acts on, instead of reacting to raw sensor noise. Everything below exists to keep that model accurate:

- **mmWave radar** — reliable stationary presence detection via HLK-LD2450 24GHz sensors paired to each ESP32-S3 node
- **Thermal imaging** — Topdon TC001 USB thermal camera for human heat detection and liveness confirmation
- **Visual camera** — Arducam OV5647 IR-CUT day/night camera for face detection via Haar cascade (InsightFace enrollment planned)
- **WiFi CSI sensing** — real Channel State Information from ESP32-S3 nodes (future research track)
- **60GHz vitals** — breathing rate and heart rate at the desk via Seeed MR60BHA2
- **EMRF Intelligence Engine** — transforms WiFi/BLE scans into person-aware presence intelligence (identity, proximity, session tracking, arrival/departure events)
- **Multi-sensor fusion** — three-layer validation pipeline (health gate → plausibility → cross-sensor consistency)
- **Sensor Fusion Inference Engine** — cross-sensor conclusion engine (planned): correlates all sensor assertions to produce human-readable situational conclusions, not raw data
- **Narrative engine** — brain service maintains living world model of home state
- **Automation** — MQTT + n8n on the Jetson for PC wake/sleep, alarm logic, and Telegram alerts

Everything runs locally. No cloud. No external APIs. No data leaves the building.

---

## Hardware

| Device | Role | IP |
|---|---|---|
| NVIDIA Jetson Orin Nano (<keep-host>) | AI hub, fusion, brain, dashboard | `$JETSON_IP` |
| Raspberry Pi 4 4GB (<broker-host>) | MQTT broker, camera adapter, face recognition | `$BROKER_IP` |
| Windows PC (<build-host>) | Development, wake/sleep target | — |
| 4x BAKODELOP ESP32-S3 N16R8 | Sentinel nodes — LD2450 radar + BLE/WiFi scan + env sensors | <node-01-ip> (node-01) |
| Arducam OV5647 Auto IR-CUT | Day/night camera — Haar face detection, 640x480 @ 1Hz | Pi CSI ribbon |
| Topdon TC001 | USB thermal camera — Y16 raw radiometric mode, 256×192 uint16 | USB → Pi (auto-detect, typically `/dev/video1` or `/dev/video2`) |
| Seeed MR60BHA2 | Desk vitals — breathing + heartbeat | WiFi direct |
| HLK-LD2450 24GHz | Per-zone multi-target tracking radar (3 targets, X/Y/speed) | UART → ESP32-S3 |
| Seeed XIAO ESP32S3 Sense | Entry snapshot camera | WiFi |

---

## Architecture

```
ESP32-S3 nodes (x4) — sentinel_node firmware (Arduino/PlatformIO)
  ├── LD2450 (UART) → multi-target tracking → UDP + MQTT → Hub
  ├── BLE/WiFi passive scan → device table → MQTT → Hub
  ├── BME688 (I2C) → environment → MQTT (Tier 1 only)
  └── SPH0645 (I2S) → acoustic presence → MQTT (Tier 1 only)

Raspberry Pi (<broker-host>) — $BROKER_IP
  ├── Mosquitto MQTT broker
  ├── Camera adapter → Arducam OV5647 (CSI) + Topdon TC001 (USB)
  │   ├── sentinel/sensors/office/camera/raw (faces, persons @ 1Hz)
  │   ├── sentinel/sensors/office/thermal/raw (heat map, blobs @ 0.5Hz)
  │   └── HTTP snapshots on :8089 (/snapshot/camera.jpg, /snapshot/thermal.jpg)
  └── InsightFace → identity confirmation (planned)

<keep-host> (Jetson) — $JETSON_IP
  ├── Node adapter → bridges home/sentinel/* → sentinel/sensors/*
  ├── Fusion service → 3-layer validation → sentinel/context/{zone}/occupancy
  ├── Brain service → narrative engine → sentinel/context/home/narrative
  ├── Dashboard → FastAPI + WebSocket on :8080 (50/50 split: floorplan + sensor panels)
  ├── n8n automation (alarm logic, Telegram, logging)
  └── CSI aggregator + signal processing pipeline (future)
```

---

## MQTT Topic Structure

### Legacy Node Topics (published by ESP32 firmware)
| Topic | Publisher | Payload |
|---|---|---|
| `home/sentinel/{node_id}/radar` | Sentinel node | `{"targets": [{x, y, speed, dist}]}` |
| `home/sentinel/{node_id}/devices` | Sentinel node | `{"wifi": [...], "ble": [...]}` |
| `home/sentinel/{node_id}/environment` | Sentinel node (Tier 1) | `{"temp_c", "humidity", "pressure_hpa", "gas_ohms"}` |
| `home/sentinel/{node_id}/acoustic` | Sentinel node (Tier 1) | `{"rms_db", "peak_db", "impulsive"}` |
| `home/sentinel/{node_id}/status` | Sentinel node | `{"uptime_s", "heap_free", "sensors": {...}}` |

### Sentinel Topic Hierarchy (after node adapter bridge)
| Layer | Topic Pattern | Publisher |
|---|---|---|
| Sensors | `sentinel/sensors/{zone}/{type}/raw` | Node adapter, camera adapter |
| Context | `sentinel/context/{zone}/occupancy` | Fusion service |
| Context | `sentinel/context/home/narrative` | Brain service |
| Identity | `sentinel/identity/{person_id}/location` | Brain (future) |
| System | `sentinel/system/{node_id}/health` | Node adapter |
| System | `sentinel/system/brain/status` | Brain service |
| System | `sentinel/system/alerts/{priority}` | Brain service |

---

## Sentinel Node Firmware Status

| Module | Status | Notes |
|---|---|---|
| **WiFi + MQTT** | ✅ Stable | Hidden SSID, auto-reconnect, LWT, OTA |
| **LD2450 Radar** | ✅ Running | 2 targets tracked, 0.9 confidence, ~1Hz |
| **BLE/WiFi Scanner** | ✅ Running | Boot loop fixed (Chat 17), 39 BLE devices in 20s |
| **Acoustic (SPH0645)** | ✅ Running | I2S @ 16kHz — not physically wired yet |
| **BME688 Environmental** | ✅ Running | 24°C, 37.8% humidity, 73K gas, ~5.5s interval |
| **YDLidar X4 Pro** | ⏳ Disabled | Code complete, waiting on powered USB hub |
| **Heartbeat/Status** | ✅ Running | 10s interval, heap monitoring |
| **toJson() fix** | ✅ Done | wifi_count/ble_count/ts serialize before device arrays (Chat 22) |

## Hub-Side Python Services

| Service | Location | Status | Notes |
|---|---|---|---|
| **Node Adapter** | <keep-host> | ✅ Live | Bridges `home/sentinel/{node_id}/*` → `sentinel/sensors/{zone}/*/raw` |
| **Camera Adapter** | <broker-host> | ✅ Running | Arducam CSI + Topdon TC001 thermal, HTTP snapshots on :8089 |
| **Fusion Service** | <keep-host> | ✅ Built | Three-layer validation, weighted fusion → occupancy. Ready to run |
| **Brain Service** | <keep-host> | ✅ Built | NarrativeEngine, world model, alerts. Ready to run |
| **Dashboard** | <keep-host> | ✅ Live | FastAPI + WS on :8080, 50/50 split — floorplan left, 7 sensor panels right (EMRF, thermal, camera, acoustic, env, radar, health) |
| **CSI Adapter** | <keep-host> | Built | Not running (CSI is future research track) |

### Sensor Trust Weights (Fusion)
| Sensor | Weight | Role |
|---|---|---|
| Radar | 0.9 | Primary presence + motion detection |
| Thermal | 0.85 | Presence + liveness confirmation |
| Camera | 0.8 | Identity + person count (visual) |
| Lidar | 0.75 | Geometry + body detection |
| CSI | 0.7 | Future: through-wall sensing |
| EMRF | 0.6 | Device-based identity + proximity (upgraded from 0.4 — intelligence engine now provides person ID, distance, session tracking) |
| Acoustic | 0.4 | Supporting evidence (activity type) |
| VOC | 0.3 | Metabolic detection |

### EMRF Intelligence Engine (Live — Chat 28)
Deployed in `sentinel/intelligence/emrf_intelligence.py`. Transforms raw WiFi/BLE device scans into:
- Per-person presence with named devices, RSSI smoothing, distance estimation, proximity bands (immediate/near/far/distant)
- Session tracking with arrival/departure detection and duration
- Zone confidence scoring (0.0–0.95)
- Three-tier device classification: known persons → infrastructure → truly unknown
- Events published on `sentinel/events/{zone}/emrf/{arrival|departure|new_device}`
- Consistent device counting: `total_devices = known_count + infra_count + unknown_count` (fixed Chat 29)

### Sensor Fusion Inference Engine (Planned — Chat 29)
The next evolution: instead of reporting raw per-sensor data, the system draws **conclusions** from cross-sensor correlation. Sensor disagreements are information, not errors.

**Architecture — Four Layers:**

| Layer | Purpose | Example |
|---|---|---|
| 1. Assertions | Each sensor publishes what it can claim | Thermal: "2 heat sources at positions X,Y" |
| 2. Correlation | Match assertions across sensors | Thermal blob A at X correlates with EMRF MAC for person's phone at similar distance |
| 3. Inference | Apply reasoning rules to correlated data | "Thermal + IR confirm presence but radar silent → person is stationary" |
| 4. Narrative | Human-readable conclusions with confidence + reasoning chain | "2 people in office. Person 1 active (typing). Person 2 stationary 15+ min. Confidence: 0.95" |

**Key Inference Rules (planned):**
- Thermal + IR confirm presence, radar silent → person stationary
- Acoustic typing count > radar movement count → seated typing activity
- EMRF device present, no thermal/IR → device left behind, person absent
- Thermal present, no EMRF → unknown person (intruder path)
- Radar spike + acoustic anomaly + no new EMRF → pet or physical event

### CSI Research Track (future)

| Phase | Goal | Status |
|---|---|---|
| **B** | Presence detection from CSI variance | Planned |
| **C** | Breathing rate extraction via FFT | Planned |
| **D** | Heart rate extraction | Planned |
| **E** | Person RF fingerprinting | Research |

---

## Repo Structure

```
project-presence/
├── sentinel_node/           # ESP32-S3 node firmware (Arduino/PlatformIO)
│   ├── sentinel_node.ino    # Main firmware
│   ├── config.h             # Node identity, WiFi, pins, timing
│   ├── network.h            # WiFi, MQTT, UDP, OTA, mDNS
│   ├── ld2450.h             # LD2450 24GHz radar UART parser
│   ├── device_scanner.h     # BLE/WiFi passive device scanner
│   ├── acoustic.h           # SPH0645 I2S acoustic presence
│   ├── bme688.h             # BME688 environmental sensor
│   ├── ydlidar.h            # YDLidar X4 Pro protocol parser
│   └── platformio.ini       # Build config (ESP32-S3, USB-CDC)
├── sentinel/                # Hub-side Python services
│   ├── adapters/
│   │   ├── node_adapter.py  # Node MQTT → sensor topic bridge (<keep-host>)
│   │   ├── camera_adapter.py# Arducam + TC001 → MQTT + HTTP (<broker-host>)
│   │   ├── csi_adapter.py   # CSI aggregator (future)
│   │   └── __main__.py      # Adapter runner (CSI + Node)
│   ├── fusion/
│   │   ├── service.py       # Three-layer validation + weighted fusion
│   │   ├── validation.py    # Health gate, plausibility, cross-sensor
│   │   ├── correlation.py   # Cross-sensor correlation engine
│   │   ├── identity_ledger.py # Person identity tracking
│   │   └── assertion_producers.py # Per-sensor assertion generators
│   ├── brain/
│   │   ├── service.py       # Primary brain — narrative orchestrator
│   │   └── narrative.py     # NarrativeEngine, occupancy state machine
│   ├── meta_reasoner/
│   │   └── service.py       # Higher-order reasoning service
│   ├── dashboard/
│   │   ├── service.py       # FastAPI + WebSocket MQTT→browser bridge
│   │   ├── index.html       # Split-view dashboard — floorplan + 7 sensor panels
│   │   └── radar_cal.html   # Radar calibration tool
│   ├── intelligence/
│   │   └── emrf_intelligence.py # EMRF: WiFi/BLE → person presence, proximity, sessions
│   ├── schemas/
│   │   ├── messages.py      # SensorReading, ZoneOccupancy, NarrativeState
│   │   ├── assertions.py    # Sensor assertion types
│   │   └── reasoning_memory.py # Anomaly, AlertEvent, ConversationFrame
│   ├── tools/
│   │   └── auto_calibrate.py # Radar auto-calibration utility
│   ├── config.py            # SentinelConfig dataclass (MQTT, zones, nodes)
│   ├── geometry.py          # Zone geometry + coordinate transforms
│   ├── topics.py            # MQTT topic hierarchy constants
│   └── watchdog.py          # Service health watchdog
├── systemd/                 # Service units for auto-start
│   ├── sentinel-node-adapter.service
│   ├── sentinel-camera-adapter.service
│   ├── sentinel-fusion.service
│   ├── sentinel-brain.service
│   └── sentinel-dashboard.service
├── bridge/                  # Python: CSI aggregator + MQTT publisher (Jetson)
├── scripts/                 # Utilities: deploy, flash, diagnostics
├── docs/                    # Architecture docs, hardware wiring, sensors
├── archive/                 # Old patch scripts, session synopses, misc
├── sentinel_config.json     # Live config (zones, nodes, MQTT)
├── DEPLOY.md                # Deploy cheat sheet (SCP + systemctl commands)
├── pyproject.toml           # Python package metadata (setuptools)
└── README.md
```

---

## Build Status

- ✅ Mosquitto MQTT broker live on <broker-host> (<broker-ip>)
- ✅ Cross-machine MQTT confirmed (<broker-host> ↔ <keep-host>)
- ✅ Wake-on-LAN confirmed (Pi → PC)
- ✅ ESP32-S3 node-01 fully operational — all sensors running, ~140-150KB heap free
- ✅ LD2450 radar live — 2 targets tracked, 0.9 confidence
- ✅ BLE/WiFi scanner live — boot loop fixed, 39 BLE devices detected
- ✅ BME688 live — temp/humidity/pressure/VOC publishing
- ✅ Node adapter live on <keep-host> — bridging node-01 → office zone topics
- ✅ Environment enrichment working — BME688 context in radar/acoustic
- ✅ Firmware toJson() reordered — counts serialize before arrays (Chat 22)
- ✅ Camera adapter built + running on <broker-host> — dual camera service
- ✅ Arducam OV5647 live — 640x480 @1fps, Haar face detection
- ✅ Topdon TC001 live — 644x384 crop-left @2fps, thermal blob detection
- ✅ TC001 YUYV frame fix — extract channel 0 from 2-channel YUYV frames
- ✅ TC001 thermal scaling fix — 8-bit luminance mapped to 15-45°C indoor range
- ✅ TC001 Y16 radiometric mode — confirmed working: 4×12621 oddball res → 2664B header + 256×192 uint16 thermal (biased 0x8000, AGC-scaled)
- ✅ TC001 frame tearing fix — grab()+retrieve() replaces read() for atomic capture
- ✅ TC001 false positive fix — thresholds tightened (30-40°C, 200px min blob)
- ✅ TC001 USB re-enumeration — auto-detect scans video0-9, handles device index changes after replug
- ⚠️ Pi undervoltage — TC001 USB power draw causes brownout; powered USB hub recommended
- ✅ HTTP snapshot server on :8089 — camera + thermal JPEGs for dashboard
- ✅ Dashboard live at :8080 — 50/50 split layout, floorplan with 18 zones left, 7 sensor panels right
- ✅ Dashboard snapshot URL fix — hardcoded <broker-host> IP for cross-host snapshot loading
- ✅ Floorplan canvas — zones drawn from imported floorplan measurements, live radar targets + occupancy overlay
- ✅ Dashboard panel order: EMRF → Thermal → Camera → Acoustic → Environment → Radar → Node Health
- ✅ Fusion service live on <keep-host>
- ✅ Brain service live on <keep-host> — narrative generating
- ✅ Systemd units for all 5 services
- ✅ EMRF Intelligence Engine deployed on <keep-host> (Chat 28) — person ID, proximity, sessions, events
- ✅ Known device MACs populated — tracked devices
- ✅ Identity tagging live — `mosquitto_sub` confirms per-person presence with device labels
- ✅ Device count consistency fix (Chat 29) — `total_devices = known + infra + unknown` now always holds
- ✅ `raw_scan_total` field added for raw WiFi+BLE scan count debugging
- ✅ Systemd 226/NAMESPACE fix — removed strict sandboxing from camera adapter unit
- ✅ Port race condition fix — `ExecStartPre` fuser kill added to prevent crash-loops
- ⏳ OTA flash remaining 3 ESP32-S3 nodes
- ⏳ Wire acoustic sensor on Node-01
- ⏳ InsightFace enrollment for identity confirmation
- ⏳ Identity mapping — narrative shows "Unknown entity" for all detections
- ⏳ SSH key auth setup across hosts
- ⏳ MR60BHA2 desk vitals sensor — to order
- ⏳ Sensor Fusion Inference Engine — cross-sensor conclusion layer (designed Chat 29, build next)

---

## Running the Services

### Camera Adapter (on <broker-host>)
```bash
PYTHONPATH=~/Presence python3 -m sentinel.adapters.camera_adapter \
    --mqtt-host <broker-ip> --zone office --thermal-device 1
```

### Node Adapter (on <keep-host>)
```bash
PYTHONPATH=~/Presence python3 -m sentinel.adapters \
    --mqtt-host <broker-ip>
```

### Fusion Service (on <keep-host>)
```bash
PYTHONPATH=~/Presence python3 -m sentinel.fusion \
    --mqtt-host <broker-ip>
```

### Brain Service (on <keep-host>)
```bash
PYTHONPATH=~/Presence python3 -m sentinel.brain \
    --mqtt-host <broker-ip>
```

### Dashboard (on <keep-host>)
```bash
PYTHONPATH=~/Presence python3 -m sentinel.dashboard \
    --mqtt-host <broker-ip> --port 8080
```

### ESP32 Firmware
```bash
cd sentinel_node
pio run -t upload    # Build + flash via USB
pio device monitor   # Serial monitor (115200 baud)
```

### OTA Flash (after initial USB flash)
```bash
# Node-01 advertises as sentinel-node-01.local via mDNS
pio run -t upload --upload-port sentinel-node-01.local
```

---

## Decisions Log

| Decision | Choice | Reason |
|---|---|---|
| Sensing backbone | HLK-LD2450 24GHz per zone | Multi-target tracking (3 targets, X/Y/speed), proven hardware |
| Thermal camera | Topdon TC001 USB | 256x192 thermal sensor, USB UVC, human-temp blob detection |
| Visual camera | Arducam OV5647 IR-CUT | Day/night via CSI ribbon, Haar cascade for face detection |
| Desk vitals | Seeed MR60BHA2 60GHz | Real breathing + heart rate at 1.5m range |
| CSI pipeline | Future research track (not RuView) | RuView is non-functional AI-generated code |
| Build system | Arduino + PlatformIO | Faster iteration than ESP-IDF, same hardware support |
| Messaging | Mosquitto MQTT on <broker-host> | Lightweight, async, all tools support it |
| Face recognition | InsightFace on Pi 4 | Runs on Pi 4, accurate, well documented |
| Fusion approach | Three-layer validation + weighted scores | Health gate → plausibility → cross-sensor consistency |
| Automation | n8n on Jetson | Already in stack, Telegram built in |
| Data residency | 100% local | Privacy by design |

---

## Repository

**GitHub:** https://github.com/jbordley/project-presence
**Local (<build-host>):** `C:\Users\<user>\Desktop\Presence`
**Deployed (<broker-host>):** `~/Presence/` (`$BROKER_IP`)
**Deployed (<keep-host>):** `~/Presence/` (`$JETSON_IP`)
**Dashboard:** `http://$JETSON_IP:8080`

*Project Presence — Living document. Last updated March 26, 2026.*
