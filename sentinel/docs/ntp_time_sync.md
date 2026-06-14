# SENTINEL Time Synchronization Setup

All timestamps in the system must be UTC epoch with millisecond precision.
Clock drift between nodes corrupts the narrative — two events that happened
simultaneously must be recognized as simultaneous.

## Architecture

```
┌─────────────────┐     NTP      ┌──────────────────┐     NTP      ┌──────────────┐
│  Internet NTP    │ ──────────→ │  Jetson (<keep-host>)│ ──────────→ │  ESP32-S3     │
│  pool.ntp.org    │             │  <jetson-ip>    │             │  nodes x4     │
└─────────────────┘             │  Local NTP Server │             │  SNTP client  │
                                └──────────────────┘             └──────────────┘
                                        │ NTP
                                        ↓
                                ┌──────────────────┐
                                │  RPi (<broker-host>)    │
                                │  <broker-ip>     │
                                └──────────────────┘
```

Jetson syncs to internet, everything else syncs to Jetson.
If internet is down, Jetson still serves as local time authority.

## Step 1: Jetson (<keep-host>) — Local NTP Server

SSH into <keep-host> and configure chrony as an NTP server:

```bash
# Install chrony (if not already)
sudo apt install -y chrony

# Edit config
sudo nano /etc/chrony/chrony.conf
```

Add/modify these lines:

```
# Sync to internet pools
pool pool.ntp.org iburst

# Serve time to local network
allow <host-ip>/24  # adjust to your subnet

# If internet is unreachable, serve local clock as fallback
local stratum 10
```

Then restart:

```bash
sudo systemctl restart chrony
sudo systemctl enable chrony

# Verify it's serving
chronyc sources
chronyc clients  # should show local clients once they connect
```

## Step 2: Raspberry Pi (<broker-host>) — NTP Client

```bash
sudo apt install -y chrony
sudo nano /etc/chrony/chrony.conf
```

Replace pool lines with:

```
server <jetson-ip> iburst prefer
pool pool.ntp.org iburst  # fallback
```

```bash
sudo systemctl restart chrony
chronyc sources  # should show <jetson-ip> as preferred
```

## Step 3: ESP32-S3 Nodes — SNTP Client

Add to `network.h` in the `begin()` method, after WiFi connects:

```cpp
#include <esp_sntp.h>

// In SentinelNetwork::begin(), after WiFi connects:
void _initTimeSync() {
    Serial.println("[NTP] Configuring SNTP...");
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, MQTT_BROKER);  // Jetson is NTP server too
    esp_sntp_setservername(1, "pool.ntp.org");  // fallback
    esp_sntp_init();

    // Wait for time sync (up to 10s)
    int retries = 0;
    while (sntp_get_sync_status() != SNTP_SYNC_STATUS_COMPLETED && retries < 20) {
        delay(500);
        retries++;
    }

    if (sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
        struct tm timeinfo;
        getLocalTime(&timeinfo);
        Serial.printf("[NTP] Time synced: %04d-%02d-%02d %02d:%02d:%02d UTC\n",
            timeinfo.tm_year + 1900, timeinfo.tm_mon + 1, timeinfo.tm_mday,
            timeinfo.tm_hour, timeinfo.tm_min, timeinfo.tm_sec);
    } else {
        Serial.println("[NTP] Time sync failed — using millis() as fallback");
    }
}

// Get UTC epoch timestamp in milliseconds
uint64_t getTimestampMs() {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (uint64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}
```

## Step 4: Timestamp in MQTT Messages

All sensor readings should include `"timestamp"` as UTC epoch seconds
with millisecond precision (e.g., `1710432000.123`).

The brain service uses these timestamps for:
- Ordering events across nodes
- Detecting simultaneous events
- Measuring transition latency between zones
- Health checking (stale timestamps = node problem)

## Drift Tolerance

| Application          | Required Precision | Notes                           |
|----------------------|--------------------|---------------------------------|
| Sensor fusion        | ±50ms              | Cross-sensor consistency checks |
| Zone transitions     | ±200ms             | Person moving between rooms     |
| Narrative ordering   | ±500ms             | Event sequence in story         |
| Health baseline       | ±1s                | Vitals averaging windows        |

SNTP on ESP32 typically achieves ±10-50ms accuracy on a LAN, which
meets all requirements.

## Verification

After setup, verify from each device:

```bash
# Jetson
chronyc tracking  # check accuracy

# RPi
chronyc sources -v  # verify Jetson is preferred source

# ESP32 (serial monitor)
# Should see "[NTP] Time synced: ..." on boot
```

On the brain service, monitor for timestamp sanity:
```bash
mosquitto_sub -t "sentinel/sensors/#" -v | python3 -c "
import sys, json, time
for line in sys.stdin:
    topic, payload = line.strip().split(' ', 1)
    ts = json.loads(payload).get('timestamp', 0)
    drift = abs(time.time() - ts)
    if drift > 1.0:
        print(f'WARNING: {topic} drift={drift:.3f}s')
"
```
