"""
Sentinel MQTT Watchdog
=======================
Runs on <keep-host> as a systemd service. Monitors:
  1. ESP32 node heartbeats (home/sentinel/node-01/status)
  2. Node adapter output (sentinel/sensors/office/*/raw)
  3. MQTT broker responsiveness

Actions on silence:
  - Logs warnings with timestamps
  - Restarts sentinel-node-adapter if it stops publishing
  - Pings ESP32 and logs if unreachable
  - Publishes watchdog health to sentinel/watchdog/status

No manual mosquitto_sub needed — this watches everything and yells when
something goes quiet.
"""

import json
import logging
import os
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("ERROR: paho-mqtt not installed. Run: pip install paho-mqtt")
    sys.exit(1)

# ── Configuration ────────────────────────────────────────────────────────

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# Timeout thresholds (seconds)
ESP32_HEARTBEAT_TIMEOUT = 120      # ESP32 should publish status every ~30-60s
ADAPTER_OUTPUT_TIMEOUT = 90        # Node adapter should publish EMRF every ~30s
BROKER_CHECK_INTERVAL = 30         # How often to evaluate health

# ESP32 node IP for ping check
ESP32_IP = os.environ.get("ESP32_IP", "localhost")

# Services the watchdog can restart
ADAPTER_SERVICE = "sentinel-node-adapter"

# How many consecutive timeouts before restarting a service
RESTART_THRESHOLD = 3
# Cooldown after restart before we check again (seconds)
RESTART_COOLDOWN = 180

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("sentinel.watchdog")


class MqttWatchdog:
    """Monitors MQTT message flow and takes corrective action on silence."""

    def __init__(self):
        self._client = mqtt.Client(client_id="sentinel-watchdog", clean_session=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

        # Last-seen timestamps
        self._last_esp32_msg = 0.0
        self._last_adapter_msg = 0.0
        self._last_any_msg = 0.0

        # Restart tracking
        self._esp32_timeout_count = 0
        self._adapter_timeout_count = 0
        self._last_adapter_restart = 0.0
        self._last_esp32_alert = 0.0

        # Message counters (reset each check interval)
        self._msg_count = 0
        self._esp32_msg_count = 0
        self._adapter_msg_count = 0

        # State
        self._connected = False
        self._running = True

    def start(self):
        """Connect to MQTT and start the watchdog loop."""
        log.info("Sentinel Watchdog starting — monitoring MQTT at %s:%d", MQTT_HOST, MQTT_PORT)
        log.info("ESP32 timeout: %ds, Adapter timeout: %ds, Check interval: %ds",
                 ESP32_HEARTBEAT_TIMEOUT, ADAPTER_OUTPUT_TIMEOUT, BROKER_CHECK_INTERVAL)

        try:
            self._client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        except Exception as e:
            log.error("Cannot connect to MQTT broker at %s:%d — %s", MQTT_HOST, MQTT_PORT, e)
            sys.exit(1)

        # Start MQTT loop in background thread
        self._client.loop_start()

        # Main watchdog loop
        try:
            while self._running:
                time.sleep(BROKER_CHECK_INTERVAL)
                self._check_health()
        except KeyboardInterrupt:
            log.info("Watchdog shutting down")
        finally:
            self._running = False
            self._client.loop_stop()
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("Connected to MQTT broker")
            self._connected = True
            now = time.time()
            self._last_esp32_msg = now
            self._last_adapter_msg = now
            self._last_any_msg = now

            # Subscribe to everything we need to monitor
            client.subscribe([
                ("home/sentinel/node-01/#", 0),        # ESP32 raw output
                ("sentinel/sensors/#", 0),              # Adapter processed output
                ("sentinel/events/#", 0),               # Events
            ])
            log.info("Subscribed to monitoring topics")

            # Publish our own status
            self._publish_status("online", "Watchdog connected and monitoring")
        else:
            log.error("MQTT connect failed with rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            log.warning("Unexpected MQTT disconnect (rc=%d) — will auto-reconnect", rc)

    def _on_message(self, client, userdata, msg):
        now = time.time()
        self._last_any_msg = now
        self._msg_count += 1

        topic = msg.topic

        # ESP32 raw messages
        if topic.startswith("home/sentinel/node-01/"):
            self._last_esp32_msg = now
            self._esp32_msg_count += 1
            self._esp32_timeout_count = 0  # reset on any message

        # Adapter processed output
        if topic.startswith("sentinel/sensors/") or topic.startswith("sentinel/events/"):
            self._last_adapter_msg = now
            self._adapter_msg_count += 1
            self._adapter_timeout_count = 0  # reset on any message

    def _check_health(self):
        """Evaluate system health and take action if needed."""
        now = time.time()

        esp32_age = now - self._last_esp32_msg
        adapter_age = now - self._last_adapter_msg

        # ── ESP32 health ──
        if esp32_age > ESP32_HEARTBEAT_TIMEOUT:
            self._esp32_timeout_count += 1
            log.warning("ESP32 SILENT for %.0fs (%d consecutive checks)",
                        esp32_age, self._esp32_timeout_count)

            # Ping check
            if self._esp32_timeout_count >= 2:
                reachable = self._ping_host(ESP32_IP)
                if reachable:
                    log.warning("ESP32 responds to ping but not publishing MQTT — "
                                "MQTT client likely stuck. Power cycle needed.")
                else:
                    log.error("ESP32 UNREACHABLE at %s — device may be powered off", ESP32_IP)

                # Alert (throttled to once per 10 min)
                if now - self._last_esp32_alert > 600:
                    self._publish_status("alert",
                        f"ESP32 node-01 silent for {int(esp32_age)}s. "
                        f"Ping={'OK' if reachable else 'FAIL'}. "
                        f"{'Power cycle needed.' if reachable else 'Check power.'}")
                    self._last_esp32_alert = now
        else:
            if self._esp32_timeout_count > 0:
                log.info("ESP32 recovered — messages flowing again")
            self._esp32_timeout_count = 0

        # ── Adapter health ──
        if adapter_age > ADAPTER_OUTPUT_TIMEOUT:
            self._adapter_timeout_count += 1
            log.warning("Node adapter SILENT for %.0fs (%d consecutive checks)",
                        adapter_age, self._adapter_timeout_count)

            # Auto-restart after threshold, with cooldown
            if (self._adapter_timeout_count >= RESTART_THRESHOLD
                    and now - self._last_adapter_restart > RESTART_COOLDOWN):
                log.warning("Restarting %s (silent for %d checks)", ADAPTER_SERVICE,
                            self._adapter_timeout_count)
                self._restart_service(ADAPTER_SERVICE)
                self._last_adapter_restart = now
                self._publish_status("restart",
                    f"Auto-restarted {ADAPTER_SERVICE} after {int(adapter_age)}s silence")
        else:
            if self._adapter_timeout_count > 0:
                log.info("Node adapter recovered — messages flowing again")
            self._adapter_timeout_count = 0

        # ── Periodic health report ──
        status_msg = (
            f"ESP32: {'OK' if esp32_age < ESP32_HEARTBEAT_TIMEOUT else f'SILENT {int(esp32_age)}s'} "
            f"({self._esp32_msg_count} msgs) | "
            f"Adapter: {'OK' if adapter_age < ADAPTER_OUTPUT_TIMEOUT else f'SILENT {int(adapter_age)}s'} "
            f"({self._adapter_msg_count} msgs) | "
            f"Total: {self._msg_count} msgs"
        )
        log.info("Health: %s", status_msg)

        # Publish health to MQTT
        self._publish_health(esp32_age, adapter_age)

        # Reset counters
        self._msg_count = 0
        self._esp32_msg_count = 0
        self._adapter_msg_count = 0

    def _ping_host(self, ip: str) -> bool:
        """Quick ping check — returns True if host responds."""
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "2", ip],
                capture_output=True, timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False

    def _restart_service(self, service_name: str):
        """Restart a systemd service."""
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", service_name],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                log.info("Successfully restarted %s", service_name)
            else:
                log.error("Failed to restart %s: %s", service_name, result.stderr.strip())
        except Exception as e:
            log.error("Exception restarting %s: %s", service_name, e)

    def _publish_status(self, level: str, message: str):
        """Publish watchdog status event."""
        if not self._connected:
            return
        payload = json.dumps({
            "level": level,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._client.publish("sentinel/watchdog/status", payload, qos=1)

    def _publish_health(self, esp32_age: float, adapter_age: float):
        """Publish periodic health metrics."""
        if not self._connected:
            return
        payload = json.dumps({
            "esp32_silent_sec": round(esp32_age, 1),
            "esp32_ok": esp32_age < ESP32_HEARTBEAT_TIMEOUT,
            "adapter_silent_sec": round(adapter_age, 1),
            "adapter_ok": adapter_age < ADAPTER_OUTPUT_TIMEOUT,
            "adapter_restart_count": self._adapter_timeout_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._client.publish("sentinel/watchdog/health", payload, qos=0)


def main():
    watchdog = MqttWatchdog()
    watchdog.start()


if __name__ == "__main__":
    main()
