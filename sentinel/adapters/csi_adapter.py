#!/usr/bin/env python3
"""
CSI-to-Sentinel Adapter
========================
Bridges the existing csi_bridge.py output (home/csi/...) into the
sentinel/ topic hierarchy. Runs alongside both services — no changes
needed to csi_bridge.py.

Subscribes to:
  home/csi/{node_id}/presence     → republishes as sentinel/sensors/{zone}/csi/raw
  home/csi/{node_id}/stats        → enriches SensorReading with health data
  home/breathing/{zone}           → includes in CSI reading payload

Publishes to:
  sentinel/sensors/{zone}/csi/raw — SensorReading messages with confidence certificates

Requires zone mapping: which node_id maps to which zone. Uses config or
falls back to csi_bridge's own zone assignments (home/csi/{node_id}/zone).

Usage:
  python -m sentinel.adapters.csi_adapter [--mqtt-host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

import paho.mqtt.client as mqtt

from sentinel.config import SentinelConfig, CONFIG_PATH
from sentinel.topics import Sensors
from sentinel.schemas.messages import SensorReading, SensorHealth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sentinel.adapter.csi")


class CSIAdapter:
    """Translates home/csi/* messages into sentinel/sensors/*/csi/raw."""

    def __init__(self, config: SentinelConfig):
        self.config = config
        self.running = False

        # Node → zone mapping (from config, updated by csi_bridge zone assigns)
        self._node_zones: dict[str, str] = {}
        for node_id, node_cfg in config.nodes.items():
            if node_cfg.zone:
                # Config uses node_id like "node_1", csi_bridge uses int "1"
                numeric_id = node_id.replace("node_", "")
                self._node_zones[numeric_id] = node_cfg.zone

        # Last known stats per node (for enriching presence messages)
        self._node_stats: dict[str, dict] = {}
        self._node_breathing: dict[str, dict] = {}

        # MQTT
        client_id = f"{config.mqtt.client_id_prefix}-csi-adapter"
        try:
            self.mqttc = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
        except (AttributeError, TypeError):
            self.mqttc = mqtt.Client(
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )

        self.mqttc.on_connect = self._on_connect
        self.mqttc.on_message = self._on_message

        if config.mqtt.username:
            self.mqttc.username_pw_set(config.mqtt.username, config.mqtt.password)

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed: rc=%d", rc)
            return

        log.info("MQTT connected")

        # Subscribe to legacy CSI topics
        client.subscribe("home/csi/+/presence", qos=0)
        client.subscribe("home/csi/+/stats", qos=0)
        client.subscribe("home/csi/+/zone", qos=0)
        client.subscribe("home/breathing/+", qos=0)

        log.info("Subscribed to home/csi/# and home/breathing/#")
        log.info("Zone mappings: %s", self._node_zones)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        topic = msg.topic
        parts = topic.split("/")

        # home/csi/{node_id}/zone — dynamic zone assignment from csi_bridge
        if len(parts) == 4 and parts[0] == "home" and parts[1] == "csi" and parts[3] == "zone":
            node_id = parts[2]
            zone = payload.get("zone", "")
            if zone:
                self._node_zones[node_id] = zone
                log.info("Zone mapping updated: node %s → %s", node_id, zone)
            return

        # home/csi/{node_id}/stats — cache for enriching readings
        if len(parts) == 4 and parts[3] == "stats":
            node_id = parts[2]
            self._node_stats[node_id] = payload
            return

        # home/breathing/{zone} — cache breathing data
        if len(parts) == 3 and parts[0] == "home" and parts[1] == "breathing":
            zone = parts[2]
            self._node_breathing[zone] = payload
            return

        # home/csi/{node_id}/presence — the main event
        if len(parts) == 4 and parts[3] == "presence":
            node_id = parts[2]
            self._translate_presence(node_id, payload)

    def _translate_presence(self, node_id: str, payload: dict):
        """Convert a csi_bridge presence message to a SensorReading."""
        zone = self._node_zones.get(node_id)
        if not zone:
            # No zone mapping yet — can't publish without knowing where
            log.debug("No zone mapping for node %s, skipping", node_id)
            return

        # Compute confidence from calibration state and variance
        stats = self._node_stats.get(node_id, {})
        calibrated = stats.get("calibrated", False)
        variance = payload.get("variance", 0.0)

        # Simple confidence: calibrated = higher base, variance adds signal
        if calibrated:
            confidence = min(0.6 + (variance / 100.0) * 0.4, 1.0)
        else:
            confidence = min(0.3 + (variance / 100.0) * 0.3, 0.7)

        # Determine health from stats
        if not stats:
            health = SensorHealth.DEGRADED.value
        elif stats.get("calibrating", False):
            health = SensorHealth.DEGRADED.value
        else:
            health = SensorHealth.NOMINAL.value

        # Merge breathing data if available for this zone
        breathing = self._node_breathing.get(zone, {})

        reading = SensorReading(
            node_id=f"node_{node_id}",
            zone=zone,
            sensor_type="csi",
            reading={
                "present": payload.get("present", False),
                "motion": payload.get("motion", False),
                "variance": variance,
                "amplitude_mean": stats.get("amplitude_mean", 0.0),
                "calibrated": calibrated,
                "breathing_bpm": breathing.get("breathing_bpm"),
                "breathing_confidence": breathing.get("confidence", 0.0),
            },
            confidence=round(confidence, 3),
            health=health,
            environment={
                "rssi": stats.get("rssi"),
                "noise_floor": stats.get("noise_floor"),
                "frame_rate_hz": stats.get("frame_rate_hz"),
            },
            physics_plausible=True,
        )

        # Publish to sentinel topic
        topic = Sensors.raw(zone, "csi")
        self.mqttc.publish(topic, reading.to_json(), qos=0)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        self.running = True
        cfg = self.config.mqtt

        log.info("Connecting to MQTT at %s:%d", cfg.host, cfg.port)
        try:
            self.mqttc.connect(cfg.host, cfg.port, keepalive=cfg.keepalive)
        except Exception:
            log.exception("Failed to connect to MQTT")
            sys.exit(1)

        self.mqttc.loop_start()
        log.info("CSI adapter started")

        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if not self.running:
            return
        self.running = False
        log.info("CSI adapter stopping...")
        self.mqttc.loop_stop()
        self.mqttc.disconnect()
        log.info("CSI adapter stopped")


def main():
    parser = argparse.ArgumentParser(description="SENTINEL CSI Adapter")
    parser.add_argument("--config", type=str, default=CONFIG_PATH)
    parser.add_argument("--mqtt-host", type=str, default=None)
    parser.add_argument("--mqtt-port", type=int, default=None)
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    config = SentinelConfig.load(args.config)
    if args.mqtt_host:
        config.mqtt.host = args.mqtt_host
    if args.mqtt_port:
        config.mqtt.port = args.mqtt_port

    adapter = CSIAdapter(config)

    def handle_signal(signum, frame):
        adapter.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    adapter.start()


if __name__ == "__main__":
    main()
