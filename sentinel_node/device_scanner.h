#pragma once
// ============================================================
// BLE/WiFi Passive Device Scanner
// Promiscuous WiFi probe sniffing + BLE advertisement scanning
// Zero hardware cost — uses ESP32-S3 built-in radios
// Conditionally compiled — BLE/WiFi stacks only linked when enabled
// ============================================================

#include <Arduino.h>
#include <ArduinoJson.h>

#if ENABLE_BLE_SCAN
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#endif

#if ENABLE_WIFI_SCAN
#include "esp_wifi.h"
#include "esp_wifi_types.h"
#endif

// --- Device entry in rolling table ---
struct DeviceEntry {
    uint8_t  mac[6];
    int8_t   rssi_latest;
    int32_t  rssi_sum;
    uint16_t seen_count;
    uint32_t first_seen;
    uint32_t last_seen;
    char     name[32];        // BLE device name or WiFi SSID
    bool     is_ble;          // true = BLE, false = WiFi
    bool     active;          // Slot in use
    uint8_t  addr_type;       // BLE address type: 0=public, 1=random, 2=rpa_public, 3=rpa_random
};

// --- Global device table (accessed from callbacks) ---
static DeviceEntry g_deviceTable[DEVICE_TABLE_MAX];
static SemaphoreHandle_t g_tableMutex = nullptr;

// ============================================================
// WiFi promiscuous callback (only compiled when WiFi scan enabled)
// ============================================================
#if ENABLE_WIFI_SCAN

// Minimal 802.11 header for probe requests
typedef struct {
    uint16_t frame_ctrl;
    uint16_t duration;
    uint8_t  addr1[6];       // Destination
    uint8_t  addr2[6];       // Source (device MAC)
    uint8_t  addr3[6];       // BSSID
    uint16_t seq_ctrl;
} __attribute__((packed)) wifi_ieee80211_mac_hdr_t;

typedef struct {
    wifi_ieee80211_mac_hdr_t hdr;
    uint8_t payload[];
} __attribute__((packed)) wifi_ieee80211_packet_t;

static void _wifiPromiscuousHandler(void* buf, wifi_promiscuous_pkt_type_t type) {
    if (type != WIFI_PKT_MGMT) return;

    const wifi_promiscuous_pkt_t* pkt = (wifi_promiscuous_pkt_t*)buf;
    const wifi_ieee80211_packet_t* ipkt = (wifi_ieee80211_packet_t*)pkt->payload;

    // Filter: only probe requests (subtype 0x04)
    uint8_t subtype = (ipkt->hdr.frame_ctrl >> 4) & 0x0F;
    if (subtype != 0x04) return;

    const uint8_t* mac = ipkt->hdr.addr2;
    int8_t rssi = pkt->rx_ctrl.rssi;

    if (xSemaphoreTake(g_tableMutex, pdMS_TO_TICKS(5))) {
        int emptySlot = -1;
        for (int i = 0; i < DEVICE_TABLE_MAX; i++) {
            if (g_deviceTable[i].active && memcmp(g_deviceTable[i].mac, mac, 6) == 0) {
                g_deviceTable[i].rssi_latest = rssi;
                g_deviceTable[i].rssi_sum += rssi;
                g_deviceTable[i].seen_count++;
                g_deviceTable[i].last_seen = millis();
                xSemaphoreGive(g_tableMutex);
                return;
            }
            if (!g_deviceTable[i].active && emptySlot < 0) emptySlot = i;
        }

        if (emptySlot >= 0) {
            DeviceEntry& e = g_deviceTable[emptySlot];
            memcpy(e.mac, mac, 6);
            e.rssi_latest = rssi;
            e.rssi_sum = rssi;
            e.seen_count = 1;
            e.first_seen = millis();
            e.last_seen = millis();
            e.name[0] = '\0';
            e.is_ble = false;
            e.active = true;
            e.addr_type = 0;  // WiFi — always public MAC
        }
        xSemaphoreGive(g_tableMutex);
    }
}

#endif // ENABLE_WIFI_SCAN

// ============================================================
// DeviceScanner class
// ============================================================
class DeviceScanner {
public:
    void begin() {
        Serial.println("[SCAN] begin() — creating mutex...");
        if (!g_tableMutex) {
            g_tableMutex = xSemaphoreCreateMutex();
        }
        if (!g_tableMutex) {
            Serial.println("[SCAN] FATAL: mutex creation failed — aborting scanner init");
            return;
        }
        memset(g_deviceTable, 0, sizeof(g_deviceTable));
        Serial.println("[SCAN] Device table cleared");

        // BLE init (heavy — allocates ~40KB, can clash with WiFi)
        #if ENABLE_BLE_SCAN
        Serial.println("[SCAN] BLE init starting...");
        BLEDevice::init("");
        _bleScan = BLEDevice::getScan();
        if (_bleScan) {
            _bleScan->setActiveScan(false);   // Passive — don't send scan requests
            _bleScan->setInterval(100);
            _bleScan->setWindow(99);
            Serial.println("[SCAN] BLE passive scanner initialized");
        } else {
            Serial.println("[SCAN] WARNING: BLE getScan() returned null");
        }
        #endif

        // WiFi promiscuous init (lightweight — hooks existing WiFi stack)
        #if ENABLE_WIFI_SCAN
        Serial.println("[SCAN] WiFi promiscuous init starting...");
        esp_err_t err = esp_wifi_set_promiscuous(true);
        if (err != ESP_OK) {
            Serial.printf("[SCAN] WARNING: esp_wifi_set_promiscuous failed: %d\n", err);
        } else {
            esp_wifi_set_promiscuous_filter(&_wifiFilter);
            esp_wifi_set_promiscuous_rx_cb(_wifiPromiscuousHandler);
            Serial.println("[SCAN] WiFi promiscuous scanner initialized");
        }
        #endif

        #if !ENABLE_BLE_SCAN && !ENABLE_WIFI_SCAN
        Serial.println("[SCAN] All scanners disabled — skeleton mode");
        #endif

        _lastPublish = millis();
        Serial.println("[SCAN] begin() complete");
    }

    void loop() {
        uint32_t now = millis();

        // Run BLE scan periodically (non-blocking, 3s windows)
        #if ENABLE_BLE_SCAN
        if (!_bleScanning && now - _lastBleScan > 20000) {
            _bleScan->start(3, false);  // 3 second scan, non-blocking
            _bleScanning = true;
            _lastBleScan = now;
        }

        if (_bleScanning && (now - _lastBleScan > 3500)) {
            _processBleResults();
            _bleScanning = false;
        }
        #endif

        // Prune stale entries
        if (now - _lastPrune > 60000) {
            _pruneStale(now);
            _lastPrune = now;
        }
    }

    // Build JSON for MQTT publish
    // Limits output to top MAX_REPORT devices per type (by RSSI) to stay
    // within MQTT buffer. Total counts reflect ALL active devices.
    static const uint8_t MAX_REPORT = 20;

    void toJson(JsonDocument& doc) {
        if (!xSemaphoreTake(g_tableMutex, pdMS_TO_TICKS(50))) return;

        // First pass: count totals and collect indices sorted by RSSI
        uint8_t wifiTotal = 0, bleTotal = 0;
        // Simple top-N selection using insertion sort into small arrays
        int16_t topWifi[MAX_REPORT];
        int16_t topBle[MAX_REPORT];
        uint8_t nWifi = 0, nBle = 0;

        for (int i = 0; i < DEVICE_TABLE_MAX; i++) {
            if (!g_deviceTable[i].active) continue;

            if (g_deviceTable[i].is_ble) {
                bleTotal++;
                _insertTop(topBle, nBle, MAX_REPORT, i, g_deviceTable[i].rssi_latest);
            } else {
                wifiTotal++;
                _insertTop(topWifi, nWifi, MAX_REPORT, i, g_deviceTable[i].rssi_latest);
            }
        }

        // Counts and timestamp FIRST — survives MQTT truncation
        doc["wifi_count"] = wifiTotal;
        doc["ble_count"]  = bleTotal;
        doc["ts"] = millis();

        // Second pass: serialize top devices (may be truncated)
        JsonArray wifi = doc["wifi"].to<JsonArray>();
        for (uint8_t j = 0; j < nWifi; j++) {
            _addDeviceJson(wifi, topWifi[j]);
        }

        JsonArray ble = doc["ble"].to<JsonArray>();
        for (uint8_t j = 0; j < nBle; j++) {
            _addDeviceJson(ble, topBle[j]);
        }

        xSemaphoreGive(g_tableMutex);
    }

    uint16_t deviceCount() {
        uint16_t count = 0;
        if (xSemaphoreTake(g_tableMutex, pdMS_TO_TICKS(10))) {
            for (int i = 0; i < DEVICE_TABLE_MAX; i++) {
                if (g_deviceTable[i].active) count++;
            }
            xSemaphoreGive(g_tableMutex);
        }
        return count;
    }

private:
    #if ENABLE_BLE_SCAN
    BLEScan* _bleScan = nullptr;
    bool     _bleScanning = false;
    uint32_t _lastBleScan = 0;
    #endif
    uint32_t _lastPublish = 0;
    uint32_t _lastPrune = 0;

    #if ENABLE_WIFI_SCAN
    static inline const wifi_promiscuous_filter_t _wifiFilter = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT
    };
    #endif

    #if ENABLE_BLE_SCAN
    void _processBleResults() {
        if (!xSemaphoreTake(g_tableMutex, pdMS_TO_TICKS(50))) return;

        BLEScanResults* results = _bleScan->getResults();
        if (!results) {
            xSemaphoreGive(g_tableMutex);
            _bleScan->clearResults();
            return;
        }
        for (int i = 0; i < results->getCount(); i++) {
            BLEAdvertisedDevice dev = results->getDevice(i);
            const uint8_t* mac = dev.getAddress().getNative();
            int8_t rssi = dev.getRSSI();

            int emptySlot = -1;
            bool found = false;
            for (int j = 0; j < DEVICE_TABLE_MAX; j++) {
                if (g_deviceTable[j].active && memcmp(g_deviceTable[j].mac, mac, 6) == 0) {
                    g_deviceTable[j].rssi_latest = rssi;
                    g_deviceTable[j].rssi_sum += rssi;
                    g_deviceTable[j].seen_count++;
                    g_deviceTable[j].last_seen = millis();
                    if (dev.haveName() && strlen(g_deviceTable[j].name) == 0) {
                        strncpy(g_deviceTable[j].name, dev.getName().c_str(), 31);
                    }
                    found = true;
                    break;
                }
                if (!g_deviceTable[j].active && emptySlot < 0) emptySlot = j;
            }

            if (!found && emptySlot >= 0) {
                DeviceEntry& e = g_deviceTable[emptySlot];
                memcpy(e.mac, mac, 6);
                e.rssi_latest = rssi;
                e.rssi_sum = rssi;
                e.seen_count = 1;
                e.first_seen = millis();
                e.last_seen = millis();
                e.is_ble = true;
                e.active = true;
                // Detect random BLE address via locally-administered bit (bit 1 of first octet)
                // BLEAddress::getType() not available in ESP32 BLE Arduino 2.0.0
                e.addr_type = (mac[0] & 0x02) ? 1 : 0;  // 1=random/local, 0=public/global
                if (dev.haveName()) {
                    strncpy(e.name, dev.getName().c_str(), 31);
                } else {
                    e.name[0] = '\0';
                }
            }
        }

        xSemaphoreGive(g_tableMutex);
        _bleScan->clearResults();
    }
    #endif

    // Insert index into top-N array sorted by RSSI (descending, strongest first)
    static void _insertTop(int16_t* arr, uint8_t& n, uint8_t maxN, int idx, int8_t rssi) {
        // Find insertion point
        uint8_t pos = n;
        for (uint8_t j = 0; j < n; j++) {
            if (rssi > g_deviceTable[arr[j]].rssi_latest) {
                pos = j;
                break;
            }
        }
        if (pos >= maxN) return;  // Weaker than all in full array
        // Shift down
        uint8_t end = (n < maxN) ? n : maxN - 1;
        for (uint8_t j = end; j > pos; j--) {
            arr[j] = arr[j - 1];
        }
        arr[pos] = idx;
        if (n < maxN) n++;
    }

    void _addDeviceJson(JsonArray& arr, int idx) {
        JsonObject obj = arr.add<JsonObject>();
        char macStr[18];
        snprintf(macStr, sizeof(macStr), "%02X:%02X:%02X:%02X:%02X:%02X",
                 g_deviceTable[idx].mac[0], g_deviceTable[idx].mac[1],
                 g_deviceTable[idx].mac[2], g_deviceTable[idx].mac[3],
                 g_deviceTable[idx].mac[4], g_deviceTable[idx].mac[5]);
        obj["mac"]  = macStr;
        obj["rssi"] = g_deviceTable[idx].rssi_latest;
        obj["seen"] = g_deviceTable[idx].seen_count;
        if (strlen(g_deviceTable[idx].name) > 0) {
            obj["name"] = g_deviceTable[idx].name;
        }
        // BLE address type: helps hub distinguish randomized MACs from stable ones
        if (g_deviceTable[idx].is_ble) {
            obj["at"] = g_deviceTable[idx].addr_type;  // 0=public, 1=random
        }
    }

    void _pruneStale(uint32_t now) {
        if (!xSemaphoreTake(g_tableMutex, pdMS_TO_TICKS(50))) return;
        for (int i = 0; i < DEVICE_TABLE_MAX; i++) {
            if (g_deviceTable[i].active &&
                (now - g_deviceTable[i].last_seen) > (DEVICE_TIMEOUT_S * 1000)) {
                g_deviceTable[i].active = false;
            }
        }
        xSemaphoreGive(g_tableMutex);
    }
};
