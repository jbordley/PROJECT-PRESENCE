// ============================================================
// Sentinel Node Firmware — v0.1
// ESP32-S3 multi-sensor presence detection node
// 7-layer sensor fusion: radar, BLE, WiFi, baro, VOC, acoustic, thermal
// ============================================================
//
// Build Priority (can be tested incrementally):
//   1. LD2450 radar + UDP        — validates radar integration
//   2. MQTT + heartbeat          — connects to hub infrastructure
//   3. BLE/WiFi passive scanner  — zero-cost device intelligence
//   4. BME688 + SPH0645          — Tier 1 environmental sensors
//
// Dependencies:
//   - ESP32 Arduino Core (ESP32-S3 board)
//   - PubSubClient
//   - ArduinoJson
//   - Adafruit BME680 library
//   - ESP32 BLE Arduino
// ============================================================

#include "ld2450.h"
#include "sentinel_net.h"
#include "device_scanner.h"

#if ENABLE_BME688
#include "bme688.h"
#endif

#if ENABLE_ACOUSTIC
#include "acoustic.h"
#endif

#if ENABLE_LIDAR
#include "ydlidar.h"
#endif

// --- Module instances ---
LD2450           radar;
SentinelNetwork  net;
DeviceScanner    scanner;

#if ENABLE_BME688
BME688Sensor     envSensor;
#endif

#if ENABLE_ACOUSTIC
AcousticSensor   acoustic;
#endif

#if ENABLE_LIDAR
YDLidar          lidar;
#endif

// --- Timing state ---
uint32_t lastRadarUdp     = 0;
uint32_t lastRadarMqtt    = 0;
uint32_t lastDevicePublish = 0;
uint32_t lastEnvPublish   = 0;
uint32_t lastAcousticPub  = 0;
uint32_t lastLidarPublish = 0;
uint32_t lastHeartbeat    = 0;
uint32_t loopCount        = 0;

// --- LED state ---
bool ledState = false;
uint32_t lastLedToggle = 0;
bool identifyMode = false;
uint32_t identifyEnd = 0;

// --- Runtime config (overridable via MQTT) ---
bool cfgRadarEnabled   = ENABLE_RADAR;
bool cfgBleEnabled     = ENABLE_BLE_SCAN;
bool cfgWifiScanEnabled = ENABLE_WIFI_SCAN;
bool cfgEnvEnabled     = ENABLE_BME688;
bool cfgAcousticEnabled = ENABLE_ACOUSTIC;
bool cfgLidarEnabled    = ENABLE_LIDAR;

// ============================================================
// MQTT Message Handler
// ============================================================
void onMqttMessage(const char* topic, const char* payload) {
    Serial.printf("[MQTT] %s → %s\n", topic, payload);

    // Command handling
    if (strstr(topic, "/command")) {
        if (strcmp(payload, "restart") == 0) {
            Serial.println("[CMD] Restarting...");
            delay(500);
            ESP.restart();
        }
        else if (strcmp(payload, "identify") == 0) {
            identifyMode = true;
            identifyEnd = millis() + 10000;  // Flash LED for 10s
            Serial.println("[CMD] Identify mode — LED flashing for 10s");
        }
        else if (strcmp(payload, "recalibrate") == 0) {
            Serial.println("[CMD] Recalibrate requested (placeholder)");
            // TODO: Implement per-sensor recalibration
        }
    }

    // Config handling
    if (strstr(topic, "/config")) {
        JsonDocument doc;
        if (deserializeJson(doc, payload) == DeserializationError::Ok) {
            if (doc["radar"].is<bool>())    cfgRadarEnabled    = doc["radar"].as<bool>();
            if (doc["ble"].is<bool>())      cfgBleEnabled      = doc["ble"].as<bool>();
            if (doc["wifi"].is<bool>())     cfgWifiScanEnabled = doc["wifi"].as<bool>();
            if (doc["env"].is<bool>())      cfgEnvEnabled      = doc["env"].as<bool>();
            if (doc["acoustic"].is<bool>()) cfgAcousticEnabled = doc["acoustic"].as<bool>();
            if (doc["lidar"].is<bool>())    cfgLidarEnabled    = doc["lidar"].as<bool>();
            Serial.println("[CFG] Config updated via MQTT");
        }
    }
}

// ============================================================
// SETUP
// ============================================================
void setup() {
    Serial.begin(115200);
    // ESP32-S3 USB-CDC needs time to enumerate after reset
    unsigned long serialWait = millis();
    while (!Serial && (millis() - serialWait < 3000)) {
        delay(10);
    }
    delay(200);

    Serial.println();
    Serial.println("=========================================");
    Serial.printf("  SENTINEL NODE — %s (Tier %d)\n", NODE_ID, NODE_TIER);
    Serial.println("=========================================");
    Serial.printf("  Firmware: v0.1 | Built: %s %s\n", __DATE__, __TIME__);
    Serial.printf("  Free heap: %u bytes\n", ESP.getFreeHeap());
    Serial.println("=========================================");

    // LED
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);

    // Network (WiFi + MQTT + UDP + OTA + mDNS)
    net.begin(onMqttMessage);

    // LD2450 Radar (UART2)
    if (cfgRadarEnabled) {
        radar.begin(Serial2);
    }

    // BLE/WiFi Device Scanner
    if (cfgBleEnabled || cfgWifiScanEnabled) {
        scanner.begin();
    }

    // BME688 Environmental (Tier 1 only)
    #if ENABLE_BME688
    if (cfgEnvEnabled) {
        if (!envSensor.begin()) {
            Serial.println("[SETUP] BME688 init failed — continuing without environmental sensing");
            cfgEnvEnabled = false;
        }
    }
    #endif

    // YDLIDAR X4 Pro (UART1)
    #if ENABLE_LIDAR
    if (cfgLidarEnabled) {
        if (lidar.begin(Serial1)) {
            lidar.startScan();
            Serial.println("[SETUP] Lidar initialized — scan starting");
        } else {
            Serial.println("[SETUP] Lidar init failed — continuing without lidar");
            cfgLidarEnabled = false;
        }
    }
    #endif

    // SPH0645 Acoustic (Tier 1 only)
    #if ENABLE_ACOUSTIC
    if (cfgAcousticEnabled) {
        if (!acoustic.begin()) {
            Serial.println("[SETUP] Acoustic init failed — continuing without acoustic sensing");
            cfgAcousticEnabled = false;
        }
    }
    #endif

    Serial.printf("\n[SETUP] Complete — sensors: radar=%d ble=%d wifi=%d env=%d acoustic=%d lidar=%d\n",
                  cfgRadarEnabled, cfgBleEnabled, cfgWifiScanEnabled,
                  cfgEnvEnabled, cfgAcousticEnabled, cfgLidarEnabled);
    Serial.printf("[SETUP] Free heap after init: %u bytes\n\n", ESP.getFreeHeap());
}

// ============================================================
// MAIN LOOP
// ============================================================
void loop() {
    uint32_t now = millis();
    loopCount++;

    // --- Network maintenance ---
    net.loop();

    // --- LED: identify mode or heartbeat blink ---
    if (identifyMode) {
        if (now > identifyEnd) {
            identifyMode = false;
            digitalWrite(LED_PIN, LOW);
        } else if (now - lastLedToggle > 200) {
            ledState = !ledState;
            digitalWrite(LED_PIN, ledState);
            lastLedToggle = now;
        }
    } else if (net.isConnected()) {
        // Slow heartbeat blink when connected
        if (now - lastLedToggle > 2000) {
            ledState = !ledState;
            digitalWrite(LED_PIN, ledState);
            lastLedToggle = now;
        }
    }

    // ==========================================================
    // 1. RADAR — LD2450 (highest priority, 10Hz UDP)
    // ==========================================================
    // Debug: check if any bytes arriving on UART2
    if (cfgRadarEnabled && loopCount % 5000 == 0) {
        Serial.printf("[RADAR] debug: frames=%u errors=%u serial_avail=%d\n",
                      radar.frameCount(), radar.errorCount(), Serial2.available());
    }
    if (cfgRadarEnabled && radar.update()) {
        // UDP: raw frames at 10Hz (low latency for real-time tracking)
        if (now - lastRadarUdp >= (1000 / RADAR_UDP_HZ)) {
            uint8_t udpBuf[64];
            size_t len = radar.toUdpPacket(udpBuf, sizeof(udpBuf));
            if (len > 0) {
                net.sendUdp(udpBuf, len);
            }
            lastRadarUdp = now;
        }

        // MQTT: JSON summary at 1Hz
        if (now - lastRadarMqtt >= (1000 / RADAR_MQTT_HZ)) {
            JsonDocument doc;
            radar.toJson(doc);
            net.publishMqttJson("radar", doc);
            lastRadarMqtt = now;

            // Debug: print targets to serial
            const RadarFrame& f = radar.frame();
            if (f.target_count > 0) {
                Serial.printf("[RADAR] %d targets:", f.target_count);
                for (int i = 0; i < LD2450_MAX_TARGETS; i++) {
                    if (f.targets[i].valid) {
                        Serial.printf(" [x=%d y=%d spd=%d dist=%d]",
                            f.targets[i].x_mm, f.targets[i].y_mm,
                            f.targets[i].speed_cms, f.targets[i].distance_mm);
                    }
                }
                Serial.println();
            }
        }
    }

    // ==========================================================
    // 2. DEVICE SCANNER — BLE/WiFi (runs in background)
    // ==========================================================
    if (cfgBleEnabled || cfgWifiScanEnabled) {
        scanner.loop();

        // Publish device table every 30s
        if (now - lastDevicePublish >= (DEVICE_PUBLISH_S * 1000)) {
            JsonDocument doc;
            scanner.toJson(doc);
            net.publishMqttJson("devices", doc);
            lastDevicePublish = now;
        }
    }

    // ==========================================================
    // 3. BME688 — Environmental (Tier 1, every 5s)
    // ==========================================================
    #if ENABLE_BME688
    if (cfgEnvEnabled) {
        if (envSensor.loop()) {
            // New reading available — publish
            if (now - lastEnvPublish >= (ENV_SAMPLE_S * 1000)) {
                JsonDocument doc;
                envSensor.toJson(doc);
                net.publishMqttJson("environment", doc);
                Serial.printf("[ENV] temp=%.1f°C humidity=%.1f%% pressure=%.2fhPa gas=%uΩ\n",
                    envSensor.temperature(), envSensor.humidity(),
                    envSensor.pressure(), (uint32_t)envSensor.gasResistance());
                lastEnvPublish = now;
            }
        }
    }
    #endif

    // ==========================================================
    // 4. ACOUSTIC — SPH0645 (Tier 1, every 1s)
    // ==========================================================
    #if ENABLE_ACOUSTIC
    if (cfgAcousticEnabled) {
        acoustic.loop();

        if (now - lastAcousticPub >= (ACOUSTIC_PUBLISH_S * 1000)) {
            JsonDocument doc;
            acoustic.toJson(doc);
            net.publishMqttJson("acoustic", doc);
            lastAcousticPub = now;
        }
    }
    #endif

    // ==========================================================
    // 5. LIDAR — YDLIDAR X4 Pro (continuous scan, publish at 2Hz)
    // ==========================================================
    #if ENABLE_LIDAR
    if (cfgLidarEnabled) {
        if (lidar.update()) {
            // New 360° scan completed
            if (now - lastLidarPublish >= (LIDAR_PUBLISH_S * 1000)) {
                // MQTT: zone summary
                JsonDocument doc;
                lidar.toJson(doc);
                net.publishMqttJson("lidar", doc);

                // UDP: compact zone distances
                uint8_t udpBuf[32];
                size_t len = lidar.toUdpPacket(udpBuf, sizeof(udpBuf));
                if (len > 0) {
                    net.sendUdp(udpBuf, len);
                }

                lastLidarPublish = now;

                // Debug
                const LidarScan& s = lidar.scan();
                Serial.printf("[LIDAR] scan #%u: %u points\n",
                              s.scan_number, s.point_count);
            }
        }

        // Periodic lidar debug
        if (loopCount % 10000 == 0) {
            Serial.printf("[LIDAR] debug: scans=%u errors=%u scanning=%d\n",
                          lidar.scanCount(), lidar.errorCount(), lidar.isScanning());
        }
    }
    #endif

    // ==========================================================
    // 6. HEARTBEAT — Status (every 10s)
    // ==========================================================
    if (now - lastHeartbeat >= (HEARTBEAT_S * 1000)) {
        net.publishHeartbeat();
        lastHeartbeat = now;

        // Periodic debug output
        Serial.printf("[STATUS] uptime=%lus heap=%u wifi=%s mqtt=%s\n",
                      now / 1000, ESP.getFreeHeap(),
                      net.isWifiConnected() ? "OK" : "DOWN",
                      net.isConnected() ? "OK" : "DOWN");

        // Scanner stats only when scanner is active
        if (cfgBleEnabled || cfgWifiScanEnabled) {
            Serial.printf("[STATUS] devices=%u\n", scanner.deviceCount());

            // Every 30s, dump scanner summary to serial
            if ((now / 1000) % 30 < HEARTBEAT_S) {
                JsonDocument scanDoc;
                scanner.toJson(scanDoc);
                Serial.printf("[SCAN] BLE=%d WiFi=%d\n",
                              scanDoc["ble_count"].as<int>(),
                              scanDoc["wifi_count"].as<int>());
            }
        }
    }

    // Small yield to prevent watchdog on tight loops
    if (loopCount % 100 == 0) {
        yield();
    }
}
