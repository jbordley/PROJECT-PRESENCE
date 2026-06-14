#pragma once
// ============================================================
// YDLIDAR X4 Pro — UART1 Point Cloud Parser + Motor Control
// 360° 2D lidar: 0.12–10m range, ~0.5° angular resolution
// Assembles full scans from streaming point cloud packets
// ============================================================

#include <Arduino.h>
#include <ArduinoJson.h>
#include "config.h"

// --- Data structures ---

struct LidarPoint {
    float    angle_deg;     // 0–359.99°
    uint16_t distance_mm;   // 0 = invalid/no return
};

struct LidarScan {
    LidarPoint points[LIDAR_MAX_POINTS];
    uint16_t   point_count;     // Valid points in this scan
    uint32_t   scan_number;     // Monotonic scan counter
    uint32_t   timestamp;       // millis() at scan completion
};

class YDLidar {
public:
    // --------------------------------------------------------
    // Lifecycle
    // --------------------------------------------------------

    bool begin(HardwareSerial& serial) {
        _serial = &serial;
        _serial->setRxBufferSize(1024);
        _serial->begin(LIDAR_BAUD, SERIAL_8N1, LIDAR_RX_PIN, LIDAR_TX_PIN);

        // Motor PWM via LEDC (legacy API for ESP32 Arduino Core v2.x)
        ledcSetup(LIDAR_MOTOR_CH, LIDAR_MOTOR_FREQ, LIDAR_MOTOR_RES);
        ledcAttachPin(LIDAR_MOTOR_PIN, LIDAR_MOTOR_CH);
        ledcWrite(LIDAR_MOTOR_CH, 0);  // Motor off initially

        _state = IDLE;
        _scanCount = 0;
        _errorCount = 0;
        _pktIdx = 0;
        _buildIdx = 0;

        Serial.printf("[LIDAR] UART1 on RX=%d TX=%d @ %d baud, motor PWM on GPIO%d\n",
                      LIDAR_RX_PIN, LIDAR_TX_PIN, LIDAR_BAUD, LIDAR_MOTOR_PIN);
        return true;
    }

    void startScan() {
        if (_state != IDLE) return;

        // Spin up motor first
        ledcWrite(LIDAR_MOTOR_CH, LIDAR_MOTOR_DUTY);
        _motorStartMs = millis();
        _state = MOTOR_SPINUP;
        Serial.println("[LIDAR] Motor starting...");
    }

    void stopScan() {
        // Send stop command
        _sendCommand(LIDAR_CMD_STOP);
        delay(10);

        // Kill motor
        ledcWrite(LIDAR_MOTOR_CH, 0);

        // Flush UART buffer
        while (_serial->available()) _serial->read();

        _state = IDLE;
        _pktState = HUNT_PH0;
        _pktIdx = 0;
        Serial.println("[LIDAR] Stopped");
    }

    // Call in loop() — drives state machine, returns true when a full 360° scan is ready
    bool update() {
        switch (_state) {
            case IDLE:
                return false;

            case MOTOR_SPINUP:
                if (millis() - _motorStartMs >= LIDAR_SPIN_UP_MS) {
                    // Check if lidar is already streaming (adapter board auto-starts)
                    int avail = _serial->available();
                    if (avail > 0) {
                        Serial.printf("[LIDAR] Already streaming (%d bytes buffered) — skipping handshake\n", avail);
                        _state = SCANNING;
                        _pktState = HUNT_PH0;
                        _pktIdx = 0;
                        _buildIdx = 0;
                        _retryCount = 0;
                        Serial.println("[LIDAR] Entering scan parse mode directly");
                    } else {
                        // Not streaming — do normal handshake
                        _sendCommand(LIDAR_CMD_SCAN);
                        _state = WAIT_RESP;
                        _respIdx = 0;
                        _respTimeoutMs = millis();
                        Serial.println("[LIDAR] Motor ready, scan command sent");
                    }
                }
                return false;

            case WAIT_RESP:
                // Consume the 7-byte response header: A5 5A 05 00 00 40 81
                while (_serial->available()) {
                    uint8_t b = _serial->read();
                    _respBuf[_respIdx++] = b;
                    if (_respIdx >= 7) {
                        // Validate response header
                        if (_respBuf[0] == 0xA5 && _respBuf[1] == 0x5A) {
                            _state = SCANNING;
                            _pktState = HUNT_PH0;
                            _pktIdx = 0;
                            _buildIdx = 0;
                            _retryCount = 0;
                            Serial.printf("[LIDAR] Scan response OK: %02X %02X %02X %02X %02X %02X %02X\n",
                                _respBuf[0], _respBuf[1], _respBuf[2], _respBuf[3],
                                _respBuf[4], _respBuf[5], _respBuf[6]);
                        } else {
                            _errorCount++;
                            Serial.printf("[LIDAR] Bad response bytes: %02X %02X %02X %02X %02X %02X %02X\n",
                                _respBuf[0], _respBuf[1], _respBuf[2], _respBuf[3],
                                _respBuf[4], _respBuf[5], _respBuf[6]);

                            if (++_retryCount <= 3) {
                                Serial.printf("[LIDAR] Retry %d/3 — flushing and resending scan command\n", _retryCount);
                                while (_serial->available()) _serial->read();
                                _sendCommand(LIDAR_CMD_STOP);
                                delay(50);
                                while (_serial->available()) _serial->read();
                                _sendCommand(LIDAR_CMD_SCAN);
                                _respIdx = 0;
                                _respTimeoutMs = millis();
                            } else {
                                Serial.println("[LIDAR] All retries failed — aborting");
                                _state = IDLE;
                                ledcWrite(LIDAR_MOTOR_CH, 0);
                                _retryCount = 0;
                            }
                        }
                        return false;
                    }
                }
                // Timeout: no response in 3 seconds
                if (millis() - _respTimeoutMs >= 3000) {
                    Serial.printf("[LIDAR] Response timeout (got %d/7 bytes) — ", _respIdx);
                    if (_respIdx > 0) {
                        for (int i = 0; i < _respIdx; i++) Serial.printf("%02X ", _respBuf[i]);
                    } else {
                        Serial.print("no data on RX");
                    }
                    Serial.println();

                    if (++_retryCount <= 3) {
                        Serial.printf("[LIDAR] Retry %d/3\n", _retryCount);
                        while (_serial->available()) _serial->read();
                        _sendCommand(LIDAR_CMD_SCAN);
                        _respIdx = 0;
                        _respTimeoutMs = millis();
                    } else {
                        Serial.println("[LIDAR] All retries failed — aborting");
                        _state = IDLE;
                        ledcWrite(LIDAR_MOTOR_CH, 0);
                        _retryCount = 0;
                    }
                }
                return false;

            case SCANNING:
                return _processStream();
        }
        return false;
    }

    const LidarScan& scan() const { return _completedScan; }
    bool isScanning() const { return _state == SCANNING; }
    bool isIdle() const { return _state == IDLE; }
    uint32_t scanCount() const { return _scanCount; }
    uint32_t errorCount() const { return _errorCount; }

    // --------------------------------------------------------
    // JSON output — summarized for MQTT
    // --------------------------------------------------------
    void toJson(JsonDocument& doc) const {
        doc["ts"] = _completedScan.timestamp;
        doc["scan"] = _completedScan.scan_number;
        doc["points"] = _completedScan.point_count;

        // Zone summary: divide 360° into 12 × 30° sectors
        // Report min distance per sector (for presence detection)
        JsonArray zones = doc["zones"].to<JsonArray>();
        for (int z = 0; z < 12; z++) {
            float zoneStart = z * 30.0f;
            float zoneEnd = zoneStart + 30.0f;
            uint16_t minDist = 0xFFFF;
            uint8_t  hitCount = 0;

            for (uint16_t i = 0; i < _completedScan.point_count; i++) {
                const LidarPoint& p = _completedScan.points[i];
                if (p.distance_mm == 0) continue;
                if (p.angle_deg >= zoneStart && p.angle_deg < zoneEnd) {
                    if (p.distance_mm < minDist) minDist = p.distance_mm;
                    hitCount++;
                }
            }

            JsonObject zo = zones.add<JsonObject>();
            zo["sector"] = z;
            zo["min_mm"] = (minDist == 0xFFFF) ? 0 : minDist;
            zo["hits"] = hitCount;
        }
    }

    // --------------------------------------------------------
    // UDP output — compact binary for real-time streaming
    // --------------------------------------------------------
    // Format: [scan_num:4] [point_count:2] [per point: angle_x10:2 dist:2]
    // Max: 4 + 2 + 720*4 = 2886 bytes — too large for single UDP packet
    // So we send zone summary instead: [scan_num:4] [12 × min_dist:2] = 28 bytes
    size_t toUdpPacket(uint8_t* buf, size_t maxLen) const {
        if (maxLen < 28) return 0;
        size_t pos = 0;

        memcpy(buf + pos, &_completedScan.scan_number, 4); pos += 4;

        for (int z = 0; z < 12; z++) {
            float zoneStart = z * 30.0f;
            float zoneEnd = zoneStart + 30.0f;
            uint16_t minDist = 0xFFFF;

            for (uint16_t i = 0; i < _completedScan.point_count; i++) {
                const LidarPoint& p = _completedScan.points[i];
                if (p.distance_mm == 0) continue;
                if (p.angle_deg >= zoneStart && p.angle_deg < zoneEnd) {
                    if (p.distance_mm < minDist) minDist = p.distance_mm;
                }
            }

            uint16_t val = (minDist == 0xFFFF) ? 0 : minDist;
            memcpy(buf + pos, &val, 2); pos += 2;
        }

        return pos;
    }

    // --------------------------------------------------------
    // Motor speed control (adjust scan frequency)
    // --------------------------------------------------------
    void setMotorDuty(uint8_t duty) {
        ledcWrite(LIDAR_MOTOR_CH, duty);
    }

private:
    HardwareSerial* _serial = nullptr;

    enum State { IDLE, MOTOR_SPINUP, WAIT_RESP, SCANNING };
    State _state = IDLE;

    uint32_t _motorStartMs = 0;
    uint8_t  _respBuf[7];
    uint8_t  _respIdx = 0;
    uint32_t _respTimeoutMs = 0;
    uint8_t  _retryCount = 0;

    uint32_t _scanCount = 0;
    uint32_t _errorCount = 0;

    // Completed scan (double-buffered: build into _buildScan, swap to _completedScan)
    LidarScan _buildScan;
    LidarScan _completedScan;
    uint16_t  _buildIdx = 0;

    // --------------------------------------------------------
    // Packet parser state machine
    // --------------------------------------------------------
    enum PktParseState { HUNT_PH0, HUNT_PH1, PKT_HEADER, PKT_BODY };
    PktParseState _pktState = HUNT_PH0;

    // Raw packet accumulation buffer
    // Max packet: 2(PH) + 1(CT) + 1(LSN) + 2(FSA) + 2(LSA) + 2(CS) + 40*2(data) = 90 bytes
    uint8_t _pktBuf[100];
    uint8_t _pktIdx = 0;
    uint8_t _pktExpectedLen = 0;

    // --------------------------------------------------------
    // Stream processing — reads UART bytes, assembles packets, builds scans
    // --------------------------------------------------------
    bool _processStream() {
        bool scanReady = false;
        int bytesRead = 0;

        while (_serial->available() && bytesRead < 256) {
            uint8_t b = _serial->read();
            bytesRead++;

            switch (_pktState) {
                case HUNT_PH0:
                    if (b == 0xAA) {  // Low byte of 0x55AA
                        _pktBuf[0] = b;
                        _pktIdx = 1;
                        _pktState = HUNT_PH1;
                    }
                    break;

                case HUNT_PH1:
                    if (b == 0x55) {  // High byte of 0x55AA
                        _pktBuf[1] = b;
                        _pktIdx = 2;
                        _pktState = PKT_HEADER;
                    } else if (b == 0xAA) {
                        // Could be start of a new header, stay looking for 0x55
                        _pktBuf[0] = b;
                        _pktIdx = 1;
                    } else {
                        _pktState = HUNT_PH0;
                    }
                    break;

                case PKT_HEADER:
                    _pktBuf[_pktIdx++] = b;
                    if (_pktIdx >= 10) {
                        // We have PH(2) + CT(1) + LSN(1) + FSA(2) + LSA(2) + CS(2) = 10 bytes
                        uint8_t lsn = _pktBuf[3];
                        if (lsn == 0 || lsn > 40) {
                            // Invalid sample count
                            _errorCount++;
                            _pktState = HUNT_PH0;
                        } else {
                            _pktExpectedLen = 10 + lsn * 2;  // Header + sample data
                            _pktState = PKT_BODY;
                        }
                    }
                    break;

                case PKT_BODY:
                    _pktBuf[_pktIdx++] = b;
                    if (_pktIdx >= _pktExpectedLen) {
                        // Full packet received — parse it
                        scanReady |= _parsePacket();
                        _pktState = HUNT_PH0;
                    }
                    break;
            }
        }

        return scanReady;
    }

    // --------------------------------------------------------
    // Parse a complete point cloud packet
    // --------------------------------------------------------
    bool _parsePacket() {
        uint8_t ct  = _pktBuf[2];
        uint8_t lsn = _pktBuf[3];
        uint16_t fsa = _pktBuf[4] | (_pktBuf[5] << 8);
        uint16_t lsa = _pktBuf[6] | (_pktBuf[7] << 8);
        uint16_t cs  = _pktBuf[8] | (_pktBuf[9] << 8);

        // Verify XOR checksum
        uint16_t check = 0;
        // XOR all 16-bit words except CS itself
        // PH
        check ^= (_pktBuf[0] | (_pktBuf[1] << 8));
        // CT | LSN
        check ^= (_pktBuf[2] | (_pktBuf[3] << 8));
        // FSA
        check ^= fsa;
        // LSA
        check ^= lsa;
        // Sample data
        for (uint8_t i = 0; i < lsn; i++) {
            uint8_t off = 10 + i * 2;
            check ^= (_pktBuf[off] | (_pktBuf[off + 1] << 8));
        }

        if (check != cs) {
            _errorCount++;
            return false;
        }

        // Decode angles (raw >> 1, then / 64.0 to get degrees)
        float startAngle = (float)(fsa >> 1) / 64.0f;
        float endAngle   = (float)(lsa >> 1) / 64.0f;

        // Handle wrap-around
        float angleDiff = endAngle - startAngle;
        if (angleDiff < 0) angleDiff += 360.0f;

        // Start-of-scan flag (CT bit 0)
        bool newScan = (ct & 0x01);

        if (newScan && _buildIdx > 0) {
            // Complete the previous scan
            _buildScan.point_count = _buildIdx;
            _buildScan.scan_number = ++_scanCount;
            _buildScan.timestamp = millis();

            // Swap buffers
            memcpy(&_completedScan, &_buildScan, sizeof(LidarScan));
            _buildIdx = 0;

            // Interpolate this packet's points into the new scan
            _addPacketPoints(lsn, startAngle, angleDiff);
            return true;  // New completed scan available
        }

        // Add points to the current building scan
        _addPacketPoints(lsn, startAngle, angleDiff);
        return false;
    }

    void _addPacketPoints(uint8_t lsn, float startAngle, float angleDiff) {
        for (uint8_t i = 0; i < lsn; i++) {
            if (_buildIdx >= LIDAR_MAX_POINTS) break;

            uint8_t off = 10 + i * 2;
            uint16_t dist = _pktBuf[off] | (_pktBuf[off + 1] << 8);

            // Filter below minimum range (X4 Pro spec: 120mm)
            if (dist > 0 && dist < LIDAR_MIN_RANGE) dist = 0;

            // Angular interpolation
            float angle;
            if (lsn > 1) {
                angle = startAngle + angleDiff * (float)i / (float)(lsn - 1);
            } else {
                angle = startAngle;
            }

            // Normalize to 0–360
            while (angle >= 360.0f) angle -= 360.0f;
            while (angle < 0.0f)    angle += 360.0f;

            _buildScan.points[_buildIdx].angle_deg = angle;
            _buildScan.points[_buildIdx].distance_mm = dist;
            _buildIdx++;
        }
    }

    // --------------------------------------------------------
    // Send command to lidar
    // --------------------------------------------------------
    void _sendCommand(uint8_t cmd) {
        uint8_t buf[2] = { LIDAR_CMD_HEADER, cmd };
        _serial->write(buf, 2);
        _serial->flush();
    }
};
