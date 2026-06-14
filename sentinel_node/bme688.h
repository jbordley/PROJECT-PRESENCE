#pragma once
// ============================================================
// BME688 Environmental Sensor — Tier 1 Nodes Only
// Temp, humidity, pressure (barometric fingerprinting), VOC
// ============================================================

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME680.h>
#include <ArduinoJson.h>
#include "config.h"

class BME688Sensor {
public:
    bool begin() {
        Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

        if (!_bme.begin(0x77, &Wire)) {
            // Try alternate address
            if (!_bme.begin(0x76, &Wire)) {
                Serial.println("[BME688] Sensor not found on 0x77 or 0x76");
                _initialized = false;
                return false;
            }
        }

        // Configure oversampling and filter
        _bme.setTemperatureOversampling(BME680_OS_8X);
        _bme.setHumidityOversampling(BME680_OS_2X);
        _bme.setPressureOversampling(BME680_OS_16X);  // High precision for baro fingerprinting
        _bme.setIIRFilterSize(BME680_FILTER_SIZE_3);
        _bme.setGasHeater(320, 150);  // 320°C for 150ms

        _initialized = true;
        _lastRead = 0;

        Serial.println("[BME688] Initialized — pressure at 16x oversample for baro fingerprinting");
        return true;
    }

    bool loop() {
        if (!_initialized) return false;

        uint32_t now = millis();
        if (now - _lastRead < (ENV_SAMPLE_S * 1000)) return false;
        _lastRead = now;

        if (!_bme.performReading()) {
            _readErrors++;
            Serial.println("[BME688] Read failed");
            return false;
        }

        _temp_c      = _bme.temperature;
        _humidity     = _bme.humidity;
        _pressure_hpa = _bme.pressure / 100.0f;   // Pa → hPa
        _gas_ohms     = _bme.gas_resistance;
        _readCount++;

        // Track pressure delta for door/window events (Section 4.4)
        if (_pressureHistory[0] > 0) {
            _pressureDelta = _pressure_hpa - _pressureHistory[_histIdx];
        }
        _pressureHistory[_histIdx] = _pressure_hpa;
        _histIdx = (_histIdx + 1) % PRESSURE_HISTORY_LEN;

        return true;
    }

    void toJson(JsonDocument& doc) const {
        doc["temp_c"]       = round2(_temp_c);
        doc["humidity"]     = round2(_humidity);
        doc["pressure_hpa"] = round2(_pressure_hpa);
        doc["gas_ohms"]     = (uint32_t)_gas_ohms;
        doc["pressure_delta"] = round2(_pressureDelta);
        doc["ts"] = millis();
    }

    // Accessors for hub-level fusion
    float temperature()    const { return _temp_c; }
    float humidity()       const { return _humidity; }
    float pressure()       const { return _pressure_hpa; }
    float gasResistance()  const { return _gas_ohms; }
    float pressureDelta()  const { return _pressureDelta; }
    bool  isInitialized()  const { return _initialized; }

private:
    Adafruit_BME680 _bme;
    bool     _initialized = false;
    uint32_t _lastRead = 0;
    uint32_t _readCount = 0;
    uint32_t _readErrors = 0;

    float _temp_c = 0;
    float _humidity = 0;
    float _pressure_hpa = 0;
    float _gas_ohms = 0;
    float _pressureDelta = 0;

    // Pressure history for delta detection (barometric fingerprinting)
    static constexpr int PRESSURE_HISTORY_LEN = 12;  // ~60s at 5s interval
    float   _pressureHistory[PRESSURE_HISTORY_LEN] = {0};
    uint8_t _histIdx = 0;

    static float round2(float v) { return roundf(v * 100.0f) / 100.0f; }
};
