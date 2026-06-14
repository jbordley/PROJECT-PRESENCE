#pragma once
// ============================================================
// Network Module — WiFi, MQTT, UDP, OTA, mDNS
// ============================================================

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <PubSubClient.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <ArduinoJson.h>

// Forward declaration for MQTT callback
typedef void (*MqttCallback)(const char* topic, const char* payload);

class SentinelNetwork {
public:
    void begin(MqttCallback onMessage = nullptr) {
        _mqttCallback = onMessage;

        // Build topic prefix
        snprintf(_topicPrefix, sizeof(_topicPrefix), "%s%s/", MQTT_PREFIX, NODE_ID);

        // WiFi
        WiFi.mode(WIFI_STA);
        WiFi.setHostname(_hostname());
        WiFi.disconnect(true);  // Clear any stale connection state
        delay(100);

#if defined(USE_STATIC_IP) && USE_STATIC_IP
        IPAddress ip, gw, sn, dns;
        ip.fromString(STATIC_IP);
        gw.fromString(STATIC_GATEWAY);
        sn.fromString(STATIC_SUBNET);
        dns.fromString(STATIC_DNS);
        WiFi.config(ip, gw, sn, dns);
        Serial.printf("[NET] Static IP configured: %s\n", STATIC_IP);
#endif

        Serial.printf("[NET] Connecting to '%s' (pass length=%d)\n", WIFI_SSID, (int)strlen(WIFI_PASS));
        WiFi.begin(WIFI_SSID, WIFI_PASS, 0, NULL, true);  // last param = hidden SSID connect

        uint32_t start = millis();
        while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
            delay(500);
            Serial.printf(".");
            // Print WiFi status every 5 seconds for debugging
            if ((millis() - start) % 5000 < 500) {
                Serial.printf(" [wifi_status=%d]", WiFi.status());
            }
        }

        if (WiFi.status() == WL_CONNECTED) {
            Serial.printf("\n[NET] Connected — IP: %s  RSSI: %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
        } else {
            Serial.printf("\n[NET] WiFi connect timeout (status=%d) — will retry in loop\n", WiFi.status());
        }

        // mDNS
        if (MDNS.begin(_hostname())) {
            Serial.printf("[NET] mDNS: %s.local\n", _hostname());
        }

        // OTA
        ArduinoOTA.setHostname(_hostname());
        ArduinoOTA.onStart([]() { Serial.println("[OTA] Start"); });
        ArduinoOTA.onEnd([]()   { Serial.println("[OTA] Done"); });
        ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
            Serial.printf("[OTA] %u%%\r", progress * 100 / total);
        });
        ArduinoOTA.onError([](ota_error_t error) {
            Serial.printf("[OTA] Error %u\n", error);
        });
        ArduinoOTA.begin();

        // MQTT
        _mqttClient.setClient(_wifiClientMqtt);
        _mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
        _mqttClient.setBufferSize(4096);
        if (_mqttCallback) {
            _mqttClient.setCallback([this](char* topic, byte* payload, unsigned int len) {
                // Null-terminate payload
                char msg[512];
                size_t copyLen = min((unsigned int)511, len);
                memcpy(msg, payload, copyLen);
                msg[copyLen] = '\0';
                _mqttCallback(topic, msg);
            });
        }

        // UDP
        _udp.begin(0); // Ephemeral local port
        _hubIP.fromString(UDP_HUB_IP);

        Serial.println("[NET] Network module initialized");
    }

    void loop() {
        // WiFi reconnect
        if (WiFi.status() != WL_CONNECTED) {
            if (millis() - _lastWifiAttempt > WIFI_RECONNECT_MS) {
                _lastWifiAttempt = millis();
                Serial.printf("[NET] WiFi retry (status=%d)...\n", WiFi.status());
#if defined(USE_STATIC_IP) && USE_STATIC_IP
                IPAddress ip, gw, sn, dns;
                ip.fromString(STATIC_IP);
                gw.fromString(STATIC_GATEWAY);
                sn.fromString(STATIC_SUBNET);
                dns.fromString(STATIC_DNS);
                WiFi.config(ip, gw, sn, dns);
#endif
                WiFi.begin(WIFI_SSID, WIFI_PASS, 0, NULL, true);  // last param = hidden SSID connect
            }
            return; // Don't do MQTT/OTA without WiFi
        }

        // OTA
        ArduinoOTA.handle();

        // MQTT reconnect
        if (!_mqttClient.connected()) {
            if (millis() - _lastMqttAttempt > MQTT_RECONNECT_MS) {
                _lastMqttAttempt = millis();
                _mqttConnect();
            }
        }
        _mqttClient.loop();
    }

    // --- Publishing ---

    bool publishMqtt(const char* subtopic, const char* payload, bool retained = false) {
        if (!_mqttClient.connected()) return false;
        char topic[128];
        snprintf(topic, sizeof(topic), "%s%s", _topicPrefix, subtopic);
        return _mqttClient.publish(topic, payload, retained);
    }

    bool publishMqttJson(const char* subtopic, JsonDocument& doc, bool retained = false) {
        // Buffer sized to match PubSubClient's 4096 MQTT buffer.
        // Previous 3072 limit caused silent drops when BLE/WiFi scan had 20+
        // devices with names (each entry ~80 bytes × 40 devices = ~3200 bytes).
        char buf[4096];
        size_t len = serializeJson(doc, buf, sizeof(buf));
        if (len == 0 || len >= sizeof(buf)) {
            Serial.printf("[NET] WARN: JSON too large for %s (%u bytes)\n", subtopic, len);
            return false;
        }
        return publishMqtt(subtopic, buf, retained);
    }

    void sendUdp(const uint8_t* data, size_t len) {
        if (!isWifiConnected()) return;  // Don't spam errors when WiFi is down
        _udp.beginPacket(_hubIP, UDP_HUB_PORT);
        // Prefix with node ID length + node ID for hub demuxing
        uint8_t idLen = strlen(NODE_ID);
        _udp.write(&idLen, 1);
        _udp.write((const uint8_t*)NODE_ID, idLen);
        _udp.write(data, len);
        _udp.endPacket();
    }

    // --- Status ---

    void publishHeartbeat() {
        JsonDocument doc;
        doc["uptime_s"]  = millis() / 1000;
        doc["heap_free"] = ESP.getFreeHeap();
        doc["wifi_rssi"] = WiFi.RSSI();
        doc["ip"]        = WiFi.localIP().toString();
        doc["hostname"]  = _hostname();        // mDNS name for OTA discovery
        doc["tier"]      = NODE_TIER;

        JsonObject sensors = doc["sensors"].to<JsonObject>();
        sensors["radar"]    = ENABLE_RADAR;
        sensors["ble"]      = ENABLE_BLE_SCAN;
        sensors["wifi"]     = ENABLE_WIFI_SCAN;
        sensors["env"]      = ENABLE_BME688;
        sensors["acoustic"] = ENABLE_ACOUSTIC;

        publishMqttJson("status", doc, true);
    }

    bool isConnected() { return WiFi.status() == WL_CONNECTED && _mqttClient.connected(); }
    bool isWifiConnected() const { return WiFi.status() == WL_CONNECTED; }

private:
    WiFiClient    _wifiClientMqtt;
    PubSubClient  _mqttClient;
    WiFiUDP       _udp;
    IPAddress     _hubIP;
    char          _topicPrefix[64];
    MqttCallback  _mqttCallback = nullptr;
    uint32_t      _lastWifiAttempt = 0;
    uint32_t      _lastMqttAttempt = 0;

    const char* _hostname() {
        static char name[32];
        snprintf(name, sizeof(name), "sentinel-%s", NODE_ID);
        return name;
    }

    void _mqttConnect() {
        char clientId[32];
        snprintf(clientId, sizeof(clientId), "sentinel-%s", NODE_ID);

        // LWT (Last Will and Testament) — offline status
        char statusTopic[128];
        snprintf(statusTopic, sizeof(statusTopic), "%sstatus", _topicPrefix);

        bool ok;
        if (strlen(MQTT_USER) > 0) {
            ok = _mqttClient.connect(clientId, MQTT_USER, MQTT_PASS,
                                     statusTopic, 1, true, "{\"online\":false}");
        } else {
            ok = _mqttClient.connect(clientId, statusTopic, 1, true, "{\"online\":false}");
        }

        if (ok) {
            Serial.printf("[MQTT] Connected to %s\n", MQTT_BROKER);

            // Subscribe to config and command topics
            char topic[128];
            snprintf(topic, sizeof(topic), "%sconfig", _topicPrefix);
            _mqttClient.subscribe(topic);
            snprintf(topic, sizeof(topic), "%scommand", _topicPrefix);
            _mqttClient.subscribe(topic);

            // Announce online
            publishMqtt("status", "{\"online\":true}", true);
        } else {
            Serial.printf("[MQTT] Connect failed, rc=%d\n", _mqttClient.state());
        }
    }
};
