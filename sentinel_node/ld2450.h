#pragma once
// ============================================================
// LD2450 24GHz Radar — UART Binary Frame Parser
// Extracts up to 3 targets: X/Y (mm), speed (cm/s), distance (mm)
// ============================================================

#include <Arduino.h>
#include <ArduinoJson.h>
#include "config.h"

struct RadarTarget {
    int16_t  x_mm;          // Signed, mm from sensor center
    int16_t  y_mm;          // Signed, mm from sensor center
    int16_t  speed_cms;     // Signed, negative = approaching
    uint16_t distance_mm;   // Computed from sqrt(x² + y²)
    uint16_t dist_resolution; // Raw bytes 6-7: range gate resolution (not actual distance)
    bool     valid;         // Target present in this slot
};

struct RadarFrame {
    RadarTarget targets[LD2450_MAX_TARGETS];
    uint8_t     target_count;
    uint32_t    timestamp;
};

class LD2450 {
public:
    void begin(HardwareSerial& serial) {
        _serial = &serial;
        _serial->setRxBufferSize(512);  // Must be set BEFORE begin()
        _serial->begin(LD2450_BAUD, SERIAL_8N1, LD2450_RX_PIN, LD2450_TX_PIN);
        _bufIdx = 0;
        _frameCount = 0;
        _errorCount = 0;
        Serial.printf("[LD2450] UART started on RX=%d TX=%d @ %d baud\n",
                      LD2450_RX_PIN, LD2450_TX_PIN, LD2450_BAUD);
    }

    // Call in loop() — reads available bytes, returns true when a new frame is ready
    bool update() {
        bool gotFrame = false;
        while (_serial->available()) {
            uint8_t b = _serial->read();
            gotFrame |= _processByte(b);
        }
        return gotFrame;
    }

    const RadarFrame& frame() const { return _frame; }
    uint32_t frameCount() const { return _frameCount; }
    uint32_t errorCount() const { return _errorCount; }

    // Serialize current frame to JSON
    void toJson(JsonDocument& doc) const {
        doc["ts"] = _frame.timestamp;
        doc["n"] = _frame.target_count;
        JsonArray arr = doc["targets"].to<JsonArray>();
        for (int i = 0; i < LD2450_MAX_TARGETS; i++) {
            if (!_frame.targets[i].valid) continue;
            JsonObject t = arr.add<JsonObject>();
            t["x"]    = _frame.targets[i].x_mm;
            t["y"]    = _frame.targets[i].y_mm;
            t["spd"]  = _frame.targets[i].speed_cms;
            t["dist"] = _frame.targets[i].distance_mm;
        }
    }

    // Compact binary format for UDP (low overhead)
    // Format: [target_count:1] [ts:4] [per target: x:2 y:2 spd:2 dist:2] = max 1+4+3*8 = 29 bytes
    size_t toUdpPacket(uint8_t* buf, size_t maxLen) const {
        if (maxLen < 5) return 0;
        size_t pos = 0;
        buf[pos++] = _frame.target_count;
        memcpy(buf + pos, &_frame.timestamp, 4); pos += 4;
        for (int i = 0; i < LD2450_MAX_TARGETS; i++) {
            if (!_frame.targets[i].valid) continue;
            if (pos + 8 > maxLen) break;
            memcpy(buf + pos, &_frame.targets[i].x_mm, 2);       pos += 2;
            memcpy(buf + pos, &_frame.targets[i].y_mm, 2);       pos += 2;
            memcpy(buf + pos, &_frame.targets[i].speed_cms, 2);  pos += 2;
            memcpy(buf + pos, &_frame.targets[i].distance_mm, 2); pos += 2;
        }
        return pos;
    }

private:
    HardwareSerial* _serial = nullptr;
    uint8_t  _buf[LD2450_FRAME_LEN];
    uint8_t  _bufIdx = 0;
    RadarFrame _frame;
    uint32_t _frameCount = 0;
    uint32_t _errorCount = 0;

    // State machine: hunt for header, accumulate frame, validate tail
    enum ParseState { HUNT_H0, HUNT_H1, HUNT_H2, HUNT_H3, BODY };
    ParseState _state = HUNT_H0;

    bool _processByte(uint8_t b) {
        switch (_state) {
            case HUNT_H0:
                if (b == LD2450_HEADER_0) { _buf[0] = b; _bufIdx = 1; _state = HUNT_H1; }
                return false;
            case HUNT_H1:
                if (b == LD2450_HEADER_1) { _buf[1] = b; _bufIdx = 2; _state = HUNT_H2; }
                else { _state = HUNT_H0; }
                return false;
            case HUNT_H2:
                if (b == LD2450_HEADER_2) { _buf[2] = b; _bufIdx = 3; _state = HUNT_H3; }
                else { _state = HUNT_H0; }
                return false;
            case HUNT_H3:
                if (b == LD2450_HEADER_3) { _buf[3] = b; _bufIdx = 4; _state = BODY; }
                else { _state = HUNT_H0; }
                return false;
            case BODY:
                _buf[_bufIdx++] = b;
                if (_bufIdx >= LD2450_FRAME_LEN) {
                    _state = HUNT_H0;
                    return _parseFrame();
                }
                return false;
        }
        return false;
    }

    bool _parseFrame() {
        // Validate tail bytes
        if (_buf[LD2450_FRAME_LEN - 2] != LD2450_TAIL_0 ||
            _buf[LD2450_FRAME_LEN - 1] != LD2450_TAIL_1) {
            _errorCount++;
            return false;
        }

        _frame.timestamp = millis();
        _frame.target_count = 0;

        // Parse 3 target blocks starting at byte 4
        // LD2450 uses sign-magnitude: bit15 = sign, bits 14-0 = magnitude
        for (int i = 0; i < LD2450_MAX_TARGETS; i++) {
            uint8_t* p = &_buf[4 + i * 8];
            uint16_t x_raw   = (uint16_t)(p[0] | (p[1] << 8));
            uint16_t y_raw   = (uint16_t)(p[2] | (p[3] << 8));
            uint16_t spd_raw = (uint16_t)(p[4] | (p[5] << 8));
            uint16_t dist    = (uint16_t)(p[6] | (p[7] << 8));

            // Sign-magnitude decode: bit15 = sign, bits 14-0 = value
            int16_t x   = (x_raw & 0x8000)   ? -(int16_t)(x_raw & 0x7FFF)   : (int16_t)(x_raw & 0x7FFF);
            int16_t y   = (y_raw & 0x8000)   ? -(int16_t)(y_raw & 0x7FFF)   : (int16_t)(y_raw & 0x7FFF);
            int16_t spd = (spd_raw & 0x8000) ? -(int16_t)(spd_raw & 0x7FFF) : (int16_t)(spd_raw & 0x7FFF);

            _frame.targets[i].x_mm = x;
            _frame.targets[i].y_mm = y;
            _frame.targets[i].speed_cms = spd;
            _frame.targets[i].dist_resolution = dist;  // raw range gate resolution
            // Compute actual distance from x,y coordinates
            _frame.targets[i].distance_mm = (uint16_t)sqrtf((float)x * x + (float)y * y);

            // Target is valid if any field is non-zero
            bool valid = (x != 0 || y != 0 || spd != 0 || dist != 0);
            _frame.targets[i].valid = valid;
            if (valid) _frame.target_count++;
        }

        _frameCount++;
        return true;
    }
};
