#pragma once
// ============================================================
// Sentinel Node — Configuration
// Copy this to config.h and edit for your deployment
// ============================================================

// --- Node Identity ---
#define NODE_ID           "node-01"          // Unique per node
#define NODE_TIER         1                  // 1 = full sensor suite, 2/3 = radar only

// --- WiFi ---
#define WIFI_SSID         "YOUR_SSID"
#define WIFI_PASS         "YOUR_PASSWORD"

// --- Hub / Broker ---
#define MQTT_BROKER       "192.168.1.X"    // Raspberry Pi hub IP
#define MQTT_PORT         1883
#define MQTT_USER         ""                 // Leave blank if no auth
#define MQTT_PASS         ""
#define UDP_HUB_IP        "192.168.1.X"
#define UDP_HUB_PORT      5005

// --- MQTT Topics (auto-prefixed with node ID) ---
#define MQTT_PREFIX       "home/sentinel/"

// --- Sensor Enables (override via MQTT config) ---
#define ENABLE_RADAR      true    // LD2450 wired — GPIO17 TX, GPIO18 RX, separate USB power
#define ENABLE_BLE_SCAN   true    // STABLE — re-enabled with defensive guards, 143K heap free
#define ENABLE_WIFI_SCAN  true    // STABLE — promiscuous mode alongside STA, no issues
#define ENABLE_BME688     true    // Wired — I2C SDA=GPIO8, SCL=GPIO9, 3.3V power from ESP32
#define ENABLE_LIDAR      true    // YDX4-Pro — GPIO15 TX, GPIO16 RX, GPIO4 motor, separate 5V power
#define ENABLE_ACOUSTIC   (NODE_TIER == 1)

// --- Pin Assignments (BAKODELOP ESP32-S3 N16R8) ---
#define LD2450_TX_PIN     17                    // UART2 TX → LD2450 RX
#define LD2450_RX_PIN     18                    // UART2 RX ← LD2450 TX
#define LIDAR_TX_PIN      15                    // UART1 TX → YDX4-Pro RX
#define LIDAR_RX_PIN      16                    // UART1 RX ← YDX4-Pro TX
#define LIDAR_MOTOR_PIN   4                     // PWM → YDX4-Pro M_CTR
#define I2C_SDA_PIN       8                     // BME688
#define I2C_SCL_PIN       9                     // BME688
#define I2S_BCLK_PIN      5                     // SPH0645 (reserved)
#define I2S_LRCK_PIN      6                     // SPH0645 (reserved)
#define I2S_DIN_PIN       7                     // SPH0645 (reserved)
#define LED_PIN           48

// --- Timing ---
#define RADAR_UDP_HZ      10                 // Raw radar → UDP rate
#define RADAR_MQTT_HZ     1                  // Summary radar → MQTT rate
#define DEVICE_PUBLISH_S  30                 // Device table → MQTT interval
#define ENV_SAMPLE_S      5                  // BME688 sample interval
#define ACOUSTIC_PUBLISH_S 1                 // Acoustic → MQTT interval
#define LIDAR_PUBLISH_S   1                  // Lidar scan → MQTT interval
#define HEARTBEAT_S       10                 // Status heartbeat interval
#define WIFI_RECONNECT_MS 5000               // WiFi retry interval
#define MQTT_RECONNECT_MS 5000               // MQTT retry interval

// --- YDLidar X4 Pro Protocol ---
#define LIDAR_BAUD        128000
#define LIDAR_PKT_HEADER  0x55AA             // Point cloud packet header
#define LIDAR_CMD_HEADER  0xA5               // Command prefix byte
#define LIDAR_CMD_SCAN    0x60               // Start scanning
#define LIDAR_CMD_STOP    0x65               // Stop scanning
#define LIDAR_CMD_INFO    0x90               // Device info request
#define LIDAR_CMD_HEALTH  0x91               // Health status request
#define LIDAR_MAX_POINTS  720                // 360° / 0.5° resolution
#define LIDAR_MIN_RANGE   120                // Minimum valid range (mm) — below this is noise
#define LIDAR_MOTOR_FREQ  25000              // PWM frequency for motor (Hz)
#define LIDAR_MOTOR_RES   8                  // PWM resolution (bits)
#define LIDAR_MOTOR_DUTY  200                // Default duty cycle (0-255)
#define LIDAR_MOTOR_CH    0                  // LEDC channel for motor PWM
#define LIDAR_SPIN_UP_MS  2000               // Motor spin-up delay before scan

// --- LD2450 Protocol ---
#define LD2450_BAUD       256000
#define LD2450_HEADER_0   0xAA
#define LD2450_HEADER_1   0xFF
#define LD2450_HEADER_2   0x03
#define LD2450_HEADER_3   0x00
#define LD2450_TAIL_0     0x55
#define LD2450_TAIL_1     0xCC
#define LD2450_MAX_TARGETS 3
#define LD2450_FRAME_LEN  30                 // 4 header + 3*8 target + 2 tail

// --- BLE/WiFi Scanner ---
#define DEVICE_TABLE_MAX  128                // Max tracked devices (39 BLE seen in 20s)
#define DEVICE_TIMEOUT_S  300                // Remove device after 5 min no-see

// --- Acoustic ---
#define I2S_SAMPLE_RATE   16000
#define I2S_BUFFER_LEN    512
#define IMPULSIVE_THRESH_DB 70.0f            // dB SPL threshold for impulsive event (clap ~75-85 dB SPL)
