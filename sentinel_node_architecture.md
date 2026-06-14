# sentinel_node.ino — Firmware Architecture

## Target Hardware
- **MCU:** ESP32-S3 (dual-core, WiFi + BLE built-in)
- **Primary sensor:** HLK-LD2450 24GHz radar (UART @ 256000 baud)
- **Secondary sensors per node variant:** BME688 (I2C), SPH0645 (I2S)
- **Comms:** UDP to Hub (sensor data), MQTT (config/commands), BLE+WiFi (passive scanning)

## Core Modules

### 1. LD2450 UART Parser (`ld2450.h`)
- Parse binary frames from LD2450 at 256000 baud on UART2
- Extract per-target: X/Y position (mm), speed (cm/s), distance resolution (mm)
- Up to 3 simultaneous targets
- Frame format: header `0xAA 0xFF 0x03 0x00` → 3x target blocks → tail `0x55 0xCC`
- Each target block: 8 bytes (X_low, X_high, Y_low, Y_high, Speed_low, Speed_high, Resolution_low, Resolution_high)
- X/Y are signed 16-bit (mm from sensor center), speed is signed (approaching = negative)
- Publish parsed targets as JSON to UDP and MQTT

### 2. BLE/WiFi Passive Scanner (`device_scanner.h`)
- **WiFi:** Promiscuous mode sniffing for probe requests — extract source MAC, RSSI, SSID
- **BLE:** Passive BLE scan — extract advertiser MAC, RSSI, device name (if broadcast)
- Maintain rolling device table: MAC → {first_seen, last_seen, rssi_avg, seen_count}
- Publish device list to MQTT every 30s: `home/sentinel/{node_id}/devices`
- Flag NEW (never-seen) vs KNOWN (in hub's registry) — hub does the matching, node just reports raw MACs

### 3. Environmental Sensor (`bme688.h`) — Tier 1 nodes only
- BME688 via I2C: temperature, humidity, pressure, gas resistance (VOC proxy)
- Sample every 5s
- Publish to MQTT: `home/sentinel/{node_id}/environment`
- Pressure values at 0.01 hPa resolution for barometric fingerprinting

### 4. Acoustic Presence (`acoustic.h`) — Tier 1 nodes only
- SPH0645 via I2S: compute RMS amplitude and spectral energy bands
- NOT streaming audio — just presence indicators (noise level, impulsive events)
- Publish to MQTT: `home/sentinel/{node_id}/acoustic`

### 5. Network & Config (`network.h`)
- WiFi STA connection with auto-reconnect
- mDNS registration as `sentinel-{node_id}.local`
- MQTT connection to hub broker
- OTA update support
- Config topics: `home/sentinel/{node_id}/config` (JSON — sample rates, scan intervals, sensor enables)
- Command topics: `home/sentinel/{node_id}/command` (recalibrate, restart, identify)
- Status topic: `home/sentinel/{node_id}/status` (heartbeat every 10s — uptime, free heap, sensor states)

## Data Flow

```
LD2450 (UART) ──→ parse ──→ UDP packet to hub (low latency, 10Hz)
                         ├──→ MQTT `home/sentinel/{node_id}/radar` (1Hz summary)
BLE scan ──────→ table ──→ MQTT `home/sentinel/{node_id}/devices` (every 30s)
WiFi scan ─────→ table ──┘
BME688 (I2C) ──→ read ───→ MQTT `home/sentinel/{node_id}/environment` (every 5s)
SPH0645 (I2S) ─→ RMS ────→ MQTT `home/sentinel/{node_id}/acoustic` (every 1s)
```

## MQTT Topic Structure

```
home/sentinel/{node_id}/radar        — JSON: targets array [{x, y, speed, dist}]
home/sentinel/{node_id}/devices      — JSON: {wifi: [{mac, rssi, ssid}], ble: [{mac, rssi, name}]}
home/sentinel/{node_id}/environment  — JSON: {temp_c, humidity, pressure_hpa, gas_ohms}
home/sentinel/{node_id}/acoustic     — JSON: {rms_db, peak_db, impulsive: bool}
home/sentinel/{node_id}/status       — JSON: {uptime_s, heap_free, sensors: {radar, ble, env, acoustic}}
home/sentinel/{node_id}/config       — JSON: writeable config (hub pushes)
home/sentinel/{node_id}/command      — string: recalibrate | restart | identify
```

## Pin Assignments (BAKODELOP ESP32-S3 N16R8)

```
UART2 TX  → GPIO17 (to LD2450 RX)
UART2 RX  → GPIO18 (from LD2450 TX)
UART1 TX  → GPIO15 (to YDX4-Pro RX)
UART1 RX  → GPIO16 (from YDX4-Pro TX)
PWM       → GPIO4  (YDX4-Pro motor M_CTR)
I2C SDA   → GPIO8  (BME688)
I2C SCL   → GPIO9  (BME688)
I2S BCLK  → GPIO5  (SPH0645, reserved)
I2S LRCK  → GPIO6  (SPH0645, reserved)
I2S DIN   → GPIO7  (SPH0645, reserved)
LED       → GPIO48 (onboard — status/identify)
```

## Build Priority
1. **LD2450 UART parser + UDP output** — this alone validates radar integration
2. **MQTT publish + status heartbeat** — connects node to hub infrastructure
3. **BLE/WiFi passive scanner** — zero-cost intelligence layer
4. **BME688 + SPH0645** — Tier 1 node sensors, can be #ifdef'd out for Tier 2/3

## Conditional Compilation
All optional sensors are gated by `#define` flags in `config.h`:
- `ENABLE_RADAR` — LD2450 (currently `true`, wired and working)
- `ENABLE_BLE_SCAN` — BLE passive scanner (currently `false`)
- `ENABLE_WIFI_SCAN` — WiFi promiscuous scanner (currently `false`)
- `ENABLE_BME688` — Environmental sensor (currently `true`, wired and working)
- `ENABLE_ACOUSTIC` — SPH0645 mic (currently `true` for Tier 1 nodes)

BLE and WiFi scanner code is additionally wrapped in `#if ENABLE_BLE_SCAN` / `#if ENABLE_WIFI_SCAN` preprocessor guards so the BLE/WiFi stacks don't link when disabled (saves ~100KB flash).

The device scanner's mutex (`g_tableMutex`) is only created when `scanner.begin()` is called. Heartbeat code guards scanner method calls behind runtime flags to avoid NULL mutex crashes.

## Dependencies
- Arduino framework (ESP32-S3 board support via PlatformIO)
- PubSubClient (MQTT)
- ArduinoJson v7
- Adafruit BME680 library (works with BME688)
- ESP32 I2S driver (built-in)

## Build
```bash
# VS Code + PlatformIO, or CLI:
pio run -t upload    # Build + flash to COM14
pio device monitor   # Serial monitor (115200, USB-CDC)
```
