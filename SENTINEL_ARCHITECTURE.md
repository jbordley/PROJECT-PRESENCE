# SENTINEL — Architecture Document

**Project Presence | Home Prototype**
Version 1.0 | March 2026

---

## 1. Project Identity

Sentinel is a privacy-first, multi-modal ambient intelligence platform that produces a continuously updated world model of who is present in a space — running entirely on local hardware with no cloud dependency.

The home prototype (Project Presence) is the field validation environment. It is not a toy build — it is a real distributed system running real hardware, producing real inference on live sensor data. The architecture is designed for platform generality from the start: the home is the first deployment environment, not the only one.

This document covers the current build.

---

## 2. Machine Inventory

| Machine | Role | IP | Hardware |
|---|---|---|---|
| **<keep-host>** | All Sentinel Python services; AI inference hub | <keep-ip> | NVIDIA Jetson Orin Nano |
| **<broker-host>** | MQTT broker (Mosquitto); video and thermal camera control | <broker-ip> | Raspberry Pi 4 4GB |
| **<build-host>** | Firmware flashing and development only | <<build-host>-ip> | Windows PC |

**Critical rule:** All `mosquitto_sub/pub` commands must use `-h <broker-ip>` regardless of originating machine. `127.0.0.1` on <keep-host> shows nothing — broker is on <broker-host>.

All production Python services run on <keep-host>.

---

## 3. Hardware — Sensor Nodes

### 3.1 Node Hardware

**Primary MCU:** BAKODELOP ESP32-S3 N16R8 (x4)
- HLK-LD2450 24GHz radar (UART @ 256000 baud) — multi-target tracking, up to 3 simultaneous targets
- BME688 environmental sensor (I2C) — temperature, humidity, pressure, gas/VOC
- SPH0645 MEMS microphone (I2S) — acoustic presence (RMS + spectral energy, not audio streaming)
- BLE + WiFi passive scanning — zero-cost device detection layer (built-in radio)

**Companion MCU (arriving April 3, 2026):** Waveshare ESP32-C6-WROOM-1-N8 (x4)
- Purpose: WiFi probe request capture (more stable than randomized MACs) + Zigbee 3.0/Thread/Matter mesh
- Augments S3 nodes — does not replace them

### 3.2 Node Deployment

| Node | Zone | Status | Radar Transform |
|---|---|---|---|
| node-01 | Office (west wall) | <host-ip> | Online, calibrated | `corrected_x = -raw_y` / `corrected_y = raw_x` |
| node-02 | Kitchen/Dining | <host-ip> | Online | Same transform as node-01 (confirmed) |
| node-03 | Family Room | <host-ip> | Online, calibration pending | Placeholder — requires stationary test |
| node-04 | Master Bedroom | <host-ip> | Online, calibration pending | No confirmed transform yet |

**Calibration rule (non-negotiable):** All radar coordinate transforms must be derived from real raw MQTT data, never guessed — derive the transform spec from live capture.

### 3.3 Firmware Architecture (`sentinel_node.ino`)

- **LD2450 parser:** Binary frame parsing at 256000 baud; extracts X/Y (signed 16-bit mm), speed, resolution per target
- **BLE/WiFi scanner:** Promiscuous mode probe sniffing + passive BLE scan; publishes rolling device table every 30s
- **Environmental:** BME688 sampled every 5s; pressure at 0.01 hPa for barometric fingerprinting
- **Acoustic:** SPH0645 RMS + spectral bands at 1s; presence indicator only, no audio
- **OTA + config push** via MQTT command topics
- **Build:** PlatformIO, VS Code, COM14; `pio run -t upload`

---

## 4. Software Architecture

### 4.1 Processing Pipeline

```
Node Adapter
    → EMRF Intelligence
        → Fusion Service
            → Correlation Engine
                → Identity Ledger
                    → Device Correlator
                        → Brain (Narrative Engine)
                            → Dashboard (LCARS)
```

### 4.2 Component Inventory

| Component | File | Location | Status |
|---|---|---|---|
| **Node Adapter** | `adapters/node_adapter.py` | <keep-host> | Running — 79% memory (101.3M/128M), OOM risk |
| **EMRF Intelligence** | `intelligence/emrf_intelligence.py` | <keep-host> | Running — wired into Node Adapter |
| **Fusion Service** | `fusion/service.py` | <keep-host> | Running |
| **Validation** | `fusion/validation.py` | <keep-host> | Running |
| **Identity Ledger** | `fusion/identity_ledger.py` | <keep-host> | Running — wired into Fusion |
| **Brain Service** | `sentinel/brain/service.py` | <keep-host> | Running (5 days) |
| **Narrative Engine** | `sentinel/brain/narrative.py` | <keep-host> | Running |
| **Dashboard** | `sentinel/dashboard/service.py` | <keep-host> | Running — FastAPI on :8080, WebSocket live |
| **Camera Adapter** | `adapters/camera_adapter.py` | <broker-host> | Running — Arducam OV5647 + Topdon TC001 |
| **Config** | `sentinel_config.json` | <keep-host> | 15 zones, 4 nodes, 6 known people, 30+ infra devices |
| **CSI Adapter** | `adapters/csi_adapter.py` | <keep-host> | Built, not deployed — experimental track only |
| **Reasoning Memory** | `schemas/reasoning_memory.py` | <keep-host> | Stubbed — schemas defined, nothing writes to it |

### 4.3 Brain Service

The Brain is the primary intelligence tier. It subscribes to all context and sensor MQTT topics, feeds the Narrative Engine, and publishes the living world model to `sentinel/context/home/narrative`.

**NarrativeEngine reasoning loop:**
```
sensor input
  → physical model update
  → narrative update
  → intent inference
  → specification check
  → action (alert / adjust / anticipate / do nothing)
```

**Known issue:** Brain `systemd` ExecStart path is wrong — service is not loading code from `~/Presence/sentinel/` on <keep-host>. Fix required before relying on Brain output.

### 4.4 Identity System

**Confidence cascade (designed; Tier 1 implemented only):**

| Tier | Method | Status |
|---|---|---|
| Tier 1 | Phone colocation — direct MAC match in EMRF Intelligence | ✅ Implemented |
| Tier 2 | Gait match ≥80% | Planned |
| Tier 3 | Routine match ≥60% | Planned |
| Tier 4 | Unknown body dot only | Fallback |

**iPhone BLE randomization** makes Tier 1 unreliable for iPhone users. WiFi RSSI causes wrong zone assignments when phone signals reach non-local nodes. Both are known limitations driving the C6 companion MCU addition and the correlation engine design.

**Enrolled people:** resident-1 (Android), resident-2 (iPhone), resident-3 (iPhone), resident-4 (no phone — pure radar/acoustic ID). resident-5 and resident-6 MACs unconfirmed — verify in `sentinel_config.json`.

### 4.5 MQTT Topic Structure

```
home/sentinel/{node_id}/radar         — targets [{x, y, speed, dist}]
home/sentinel/{node_id}/devices       — {wifi: [...], ble: [...]}
home/sentinel/{node_id}/environment   — {temp_c, humidity, pressure_hpa, gas_ohms}
home/sentinel/{node_id}/acoustic      — {rms_db, peak_db, impulsive: bool}
home/sentinel/{node_id}/status        — heartbeat (10s)
home/sentinel/{node_id}/config        — writeable config (hub pushes)
home/sentinel/{node_id}/command       — recalibrate | restart | identify

sentinel/identity/{person_id}/location — Identity Ledger output (primary person placement)
sentinel/context/{zone}/occupancy      — Zone occupancy
sentinel/context/home/narrative        — Brain world model output
sentinel/system/brain/status           — Brain heartbeat
sentinel/system/alerts/{priority}      — Alert output

home/csi/{node_id}/stats              — CSI experimental track (see Section 5)
home/presence/{zone}                  — CSI presence output (experimental)
home/breathing/{zone}                 — CSI breathing rate (experimental)
```

---

## 5. CSI — Experimental Parallel Track

WiFi CSI sensing is an active experimental track, **not** the presence detection backbone.

**What exists:** `bridge/csi_bridge.py` — Phase A (raw CSI capture + UDP), Phase B (amplitude variance presence detection), Phase C (breathing rate via FFT bandpass) are all implemented and running on <keep-host> as a `systemd` service.

**What it is not:** RuView has been abandoned — it produced fake/hardcoded CSI data. The current CSI pipeline is a custom ESP-IDF implementation.

**Status:** Runs independently of the main Sentinel pipeline. Does not feed the Fusion Service or Brain. Publishes to `home/csi/` and `home/presence/` topics only.

**Future integration path:** Phase E (RF fingerprinting) could contribute to the identity ledger as a Tier 2+ signal, but this is research, not a build target.

---

## 6. Security Architecture

**Inter-node (planned):** mTLS on MQTT. Every node holds a certificate from a local CA on <keep-host> (step-ca or cfssl). No node connects without a valid cert. Physical access to <keep-host> required for enrollment.

**Human ↔ Brain channels:**
- Local HTTPS API — token authenticated, serves dashboard
- Telegram bot — end-to-end encrypted, works off-network, via n8n
- MCP server — authenticated agent tool invocation (planned)

**Data protection:** Biometric profiles encrypted at rest on <keep-host>. World model digests encrypted before transmission.

---

## 7. Distributed Memory Architecture

The Hub (Brain) is a peer node with coordination role — not a separate tier above nodes. Memory is distributed holographically:

| Layer | Scope | Storage |
|---|---|---|
| Local Experience | Own zone only | SQLite on-node, full resolution |
| System Digest | All zones, compressed | SQLite, synced from peers |
| Reasoning Log | Own conclusions | Append-only log on-node |

Adjacent nodes share observations via lateral MQTT peer topics (`home/node/{id}/peer/{neighbor_id}`) without waiting for Brain. Brain synthesizes; it does not gatekeep.

**Resilience:** Brain failure → nodes continue autonomously, sync on reconnect. Node failure → peers retain compressed representation of that zone.

---

## 8. Three-Tier Agent Architecture (Planned)

| Tier | Engine | Description |
|---|---|---|
| Sentinel Basic | Rule engine | Explicit rules, no LLM dependency, zero inference cost |
| Sentinel Pro | Ollama (local LLM on <keep-host>) | Contextual reasoning over world model |
| Sentinel Elite | Cloud LLM | Explainable reasoning, premium response quality |

Current build: rule engine only. Ollama integration is a Phase 2 target.

---

## 9. Known Limitations & Roadmap

| Item | Status | Notes |
|---|---|---|
| iPhone BLE MAC randomization | Known limitation | Breaks Tier-1 phone-colocation identity for iPhone users; drives the C6 companion MCU (probe capture) and the correlation engine design |
| Radar ghosts / phantom occupants | Mitigation in progress | Radar reflections and randomized BLE MACs can inflate occupant counts; cap total occupants to a plausible household max |
| Zone thrashing | Mitigation in progress | Require N consecutive readings before a zone transition to stop rapid bouncing between adjacent zones |
| Nodes 3 & 4 radar calibration | Pending | Placeholder transforms — require a stationary calibration test per the calibration rule (Section 3.2) |
| ESP32-C6 companion firmware | Planned | WiFi probe-request capture + Zigbee/Thread/Matter mesh |
| mTLS between nodes | Planned | Config fields exist, unused — LAN-only for now |
| Reasoning Memory | Planned | Schemas and interface defined; DecisionRecord writing not yet wired |
| Brain `systemd` ExecStart path | Fix required | Service must load code from the deployed Sentinel path before relying on Brain output |
