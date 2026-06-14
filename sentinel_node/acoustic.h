#pragma once
// ============================================================
// SPH0645 Acoustic Presence — Tier 1 Nodes Only
// I2S microphone → RMS/peak amplitude, impulsive event detection
// NOT streaming audio — just presence indicators
// ============================================================

#include <Arduino.h>
#include <driver/i2s.h>
#include <ArduinoJson.h>
#include <math.h>
#include "config.h"

class AcousticSensor {
public:
    bool begin() {
        i2s_config_t i2sConfig = {
            .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
            .sample_rate = I2S_SAMPLE_RATE,
            .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,  // SPH0645 outputs 18-bit in 32-bit frame
            .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
            .communication_format = I2S_COMM_FORMAT_STAND_I2S,
            .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
            .dma_buf_count = 4,
            .dma_buf_len = I2S_BUFFER_LEN,
            .use_apll = false,
            .tx_desc_auto_clear = false,
            .fixed_mclk = 0
        };

        i2s_pin_config_t pinConfig = {
            .bck_io_num   = I2S_BCLK_PIN,
            .ws_io_num    = I2S_LRCK_PIN,
            .data_out_num = I2S_PIN_NO_CHANGE,
            .data_in_num  = I2S_DIN_PIN
        };

        esp_err_t err = i2s_driver_install(I2S_NUM_0, &i2sConfig, 0, NULL);
        if (err != ESP_OK) {
            Serial.printf("[ACOUSTIC] I2S driver install failed: %d\n", err);
            return false;
        }

        err = i2s_set_pin(I2S_NUM_0, &pinConfig);
        if (err != ESP_OK) {
            Serial.printf("[ACOUSTIC] I2S pin config failed: %d\n", err);
            return false;
        }

        i2s_zero_dma_buffer(I2S_NUM_0);
        _initialized = true;
        _lastPublish = millis();

        Serial.printf("[ACOUSTIC] I2S started — %dHz, buffer %d samples\n",
                      I2S_SAMPLE_RATE, I2S_BUFFER_LEN);
        return true;
    }

    // Call in loop — reads I2S buffer, computes RMS
    bool loop() {
        if (!_initialized) return false;

        size_t bytesRead = 0;
        int32_t samples[I2S_BUFFER_LEN];

        esp_err_t err = i2s_read(I2S_NUM_0, samples, sizeof(samples), &bytesRead, pdMS_TO_TICKS(10));
        if (err != ESP_OK || bytesRead == 0) return false;

        int numSamples = bytesRead / sizeof(int32_t);
        if (numSamples == 0) return false;

        // SPH0645 data is in upper 18 bits of 32-bit word, shift right 14
        // First pass: compute DC offset (mean) to remove bias
        double sum = 0;
        for (int i = 0; i < numSamples; i++) {
            sum += (double)(samples[i] >> 14);
        }
        double dcOffset = sum / numSamples;

        // Second pass: compute RMS and peak with DC removed
        double sumSq = 0;
        int32_t peak = 0;
        for (int i = 0; i < numSamples; i++) {
            int32_t s = samples[i] >> 14;  // Normalize to ~18-bit range
            double ac = (double)s - dcOffset;  // Remove DC bias
            sumSq += ac * ac;
            int32_t absAc = abs((int32_t)ac);
            if (absAc > peak) peak = absAc;
        }

        double rms = sqrt(sumSq / numSamples);

        // Convert to dB SPL (approximate — SPH0645 sensitivity is -26 dBFS = 94 dB SPL)
        // Reference: full-scale 18-bit = 131072. At -26 dBFS, 94 dB SPL maps to ~6554 counts.
        // dB SPL ≈ 20*log10(rms/6554) + 94
        const double refCounts = 6554.0;  // counts at 94 dB SPL (-26 dBFS sensitivity)
        _rmsDb  = (rms > 0) ? 20.0f * log10f((float)(rms / refCounts)) + 94.0f : 0;
        _peakDb = (peak > 0) ? 20.0f * log10f((float)(peak / refCounts)) + 94.0f : 0;

        // Impulsive event detection: sudden spike above threshold
        bool impulsive = (_peakDb > IMPULSIVE_THRESH_DB && _peakDb > _prevPeakDb + 10.0f);
        _impulsive = impulsive;
        _prevPeakDb = _peakDb;

        // Rolling average for ambient baseline
        _ambientSum += _rmsDb;
        _ambientCount++;
        if (_ambientCount >= 100) {
            _ambientDb = _ambientSum / _ambientCount;
            _ambientSum = 0;
            _ambientCount = 0;
        }

        return true;
    }

    void toJson(JsonDocument& doc) const {
        doc["rms_db"]      = round1(_rmsDb);
        doc["peak_db"]     = round1(_peakDb);
        doc["ambient_db"]  = round1(_ambientDb);
        doc["impulsive"]   = _impulsive;
        doc["ts"]          = millis();
    }

    float rmsDb()         const { return _rmsDb; }
    float peakDb()        const { return _peakDb; }
    bool  impulsive()     const { return _impulsive; }
    bool  isInitialized() const { return _initialized; }

private:
    bool     _initialized = false;
    uint32_t _lastPublish = 0;
    float    _rmsDb = 0;
    float    _peakDb = 0;
    float    _prevPeakDb = 0;
    float    _ambientDb = 0;
    double   _ambientSum = 0;
    uint32_t _ambientCount = 0;
    bool     _impulsive = false;

    static float round1(float v) { return roundf(v * 10.0f) / 10.0f; }
};
