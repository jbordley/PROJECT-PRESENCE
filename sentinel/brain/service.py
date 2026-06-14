#!/usr/bin/env python3
"""
SENTINEL Brain Service
=======================
The primary brain (Tier 3). Subscribes to all context and sensor topics,
maintains the narrative via NarrativeEngine, and publishes the living
world model to sentinel/context/home/narrative.

Also publishes:
  - sentinel/system/brain/status (heartbeat)
  - sentinel/system/alerts/{priority} (when anomalies require action)

Usage:
  python -m sentinel.brain.service [--config path/to/config.json]

Or as a systemd service (see sentinel_brain.service).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import threading
from typing import Optional

import paho.mqtt.client as mqtt

from sentinel.config import SentinelConfig, CONFIG_PATH
from sentinel.topics import Sensors, Context, System, Identity
from sentinel.schemas.messages import (
    SensorReading,
    ZoneOccupancy,
    NarrativeState,
    BrainStatus,
    Alert,
    AlertPriority,
)
from sentinel.brain.narrative import NarrativeEngine

# ── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sentinel.brain")


class BrainService:
    """
    Primary brain service. Wires MQTT subscriptions to the NarrativeEngine
    and publishes the narrative as the single source of truth.
    """

    def __init__(self, config: SentinelConfig):
        self.config = config

        # Build person_id → display name map from config
        name_map = {}
        for person_id, person_data in config.known_devices.items():
            name_map[person_id] = person_data.get("name", person_id.replace("_", " ").title())

        self.narrative = NarrativeEngine(_name_map=name_map)
        self.running = False
        self._start_time = time.time()
        self._lock = threading.Lock()

        # MQTT client
        client_id = f"{config.mqtt.client_id_prefix}-brain"
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

        # Callbacks
        self.mqttc.on_connect = self._on_connect
        self.mqttc.on_message = self._on_message
        self.mqttc.on_disconnect = self._on_disconnect

        # Last will — offline status on unexpected disconnect
        self.mqttc.will_set(
            System.brain_status(),
            BrainStatus(status="offline").to_json(),
            qos=1,
            retain=True,
        )

        # Auth (if configured)
        if config.mqtt.username:
            self.mqttc.username_pw_set(config.mqtt.username, config.mqtt.password)

        # Publish timers
        self._last_narrative_publish = 0.0
        self._last_health_publish = 0.0

    # ── MQTT Callbacks ────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connection failed: rc=%d", rc)
            return

        log.info("MQTT connected to %s:%d", self.config.mqtt.host, self.config.mqtt.port)

        # Publish online status
        client.publish(
            System.brain_status(),
            BrainStatus(
                status="online",
                tier=self.config.brain_tier,
                zones_tracked=list(self.config.zones.keys()),
            ).to_json(),
            qos=1,
            retain=True,
        )

        # Subscribe to all sensor raw data (for Stage 1 direct processing)
        client.subscribe(Sensors.raw_wildcard_all(), qos=1)
        log.info("Subscribed: %s", Sensors.raw_wildcard_all())

        # Subscribe to all zone context (for when fusion service exists)
        client.subscribe(Context.occupancy_wildcard(), qos=1)
        log.info("Subscribed: %s", Context.occupancy_wildcard())

        client.subscribe(Context.state_wildcard(), qos=1)
        log.info("Subscribed: %s", Context.state_wildcard())

        # Subscribe to node health
        client.subscribe(System.health_wildcard(), qos=1)
        log.info("Subscribed: %s", System.health_wildcard())

        log.info("Brain service online — Tier %d, tracking %d zones",
                 self.config.brain_tier, len(self.config.zones))

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly: rc=%d", rc)
        else:
            log.info("MQTT disconnected cleanly")

    def _on_message(self, client, userdata, msg):
        """Route incoming messages to appropriate handlers."""
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.debug("Ignoring non-JSON message on %s: %s", msg.topic, e)
            return

        topic = msg.topic

        with self._lock:
            # ── Sensor raw data (Stage 1: brain processes directly) ───────
            if topic.startswith("sentinel/sensors/"):
                self._handle_sensor_reading(topic, payload)

            # ── Zone occupancy (from fusion service, Stage 2+) ────────────
            elif topic.startswith("sentinel/context/") and topic.endswith("/occupancy"):
                self._handle_zone_occupancy(topic, payload)

            # ── Node health ───────────────────────────────────────────────
            elif topic.startswith("sentinel/system/") and topic.endswith("/health"):
                self._handle_node_health(topic, payload)

    # ── Message Handlers ──────────────────────────────────────────────────

    def _handle_sensor_reading(self, topic: str, payload: dict):
        """
        Handle raw sensor data. In Stage 1, the brain acts as its own
        lightweight fusion layer — processing sensor readings directly.
        When the fusion service comes online (Stage 2), it will publish
        to context topics and the brain switches to those.
        """
        try:
            reading = SensorReading.from_dict(payload)
        except Exception as e:
            log.debug("Failed to parse sensor reading: %s", e)
            return

        # Extract zone and sensor type from topic if not in payload
        # sentinel/sensors/{zone}/{sensor_type}/raw
        parts = topic.split("/")
        if len(parts) >= 5:
            if not reading.zone:
                reading.zone = parts[2]
            if not reading.sensor_type:
                reading.sensor_type = parts[3]

        result = self.narrative.process_sensor_reading(reading)
        if result:
            self._maybe_publish_narrative()

    def _handle_zone_occupancy(self, topic: str, payload: dict):
        """Handle interpreted zone occupancy from fusion service."""
        try:
            occ = ZoneOccupancy.from_dict(payload)
        except Exception as e:
            log.debug("Failed to parse zone occupancy: %s", e)
            return

        # Extract zone from topic if not in payload
        parts = topic.split("/")
        if len(parts) >= 4 and not occ.zone:
            occ.zone = parts[2]

        self.narrative.process_zone_occupancy(occ)
        self._maybe_publish_narrative()

    def _handle_node_health(self, topic: str, payload: dict):
        """Track node health for brain status reporting."""
        # Stage 1: just log. Stage 5: integrate into reliability scoring.
        node_id = payload.get("node_id", "unknown")
        log.debug("Node health: %s", node_id)

    # ── Publishing ────────────────────────────────────────────────────────

    def _maybe_publish_narrative(self):
        """Rate-limited narrative publish."""
        now = time.time()
        if now - self._last_narrative_publish < self.config.narrative_publish_interval:
            return

        self._last_narrative_publish = now
        state = self.narrative.get_state()

        self.mqttc.publish(
            Context.narrative(),
            state.to_json(),
            qos=0,
            retain=True,
        )
        log.debug("Narrative v%d: %s", state.narrative_version, state.summary)

    def _publish_brain_status(self):
        """Periodic brain health heartbeat."""
        state = self.narrative.get_state()
        status = BrainStatus(
            status="online",
            tier=self.config.brain_tier,
            uptime_sec=round(time.time() - self._start_time, 1),
            narrative_version=state.narrative_version,
            zones_tracked=list(self.config.zones.keys()),
            nodes_online=0,   # TODO: track from health messages
            nodes_degraded=0,
        )

        self.mqttc.publish(
            System.brain_status(),
            status.to_json(),
            qos=1,
            retain=True,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        """Connect to MQTT and start the brain service."""
        self.running = True
        cfg = self.config.mqtt

        log.info("Connecting to MQTT at %s:%d", cfg.host, cfg.port)
        try:
            self.mqttc.connect(cfg.host, cfg.port, keepalive=cfg.keepalive)
        except Exception:
            log.exception("Failed to connect to MQTT broker")
            sys.exit(1)

        self.mqttc.loop_start()

        log.info("Brain service started — entering main loop")
        try:
            while self.running:
                now = time.time()

                # Periodic health publish
                if now - self._last_health_publish >= self.config.health_publish_interval:
                    self._last_health_publish = now
                    self._publish_brain_status()

                time.sleep(0.1)

        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        """Graceful shutdown."""
        if not self.running:
            return
        self.running = False
        log.info("Brain service shutting down...")

        # Final offline status
        self.mqttc.publish(
            System.brain_status(),
            BrainStatus(status="offline").to_json(),
            qos=1,
            retain=True,
        )

        self.mqttc.loop_stop()
        self.mqttc.disconnect()
        log.info("Brain service stopped")


# ── CLI Entry Point ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SENTINEL Brain Service")
    parser.add_argument(
        "--config", type=str, default=CONFIG_PATH,
        help=f"Path to config JSON (default: {CONFIG_PATH})"
    )
    parser.add_argument(
        "--mqtt-host", type=str, default=None,
        help="Override MQTT host from config"
    )
    parser.add_argument(
        "--mqtt-port", type=int, default=None,
        help="Override MQTT port from config"
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    config = SentinelConfig.load(args.config)
    if args.mqtt_host:
        config.mqtt.host = args.mqtt_host
    if args.mqtt_port:
        config.mqtt.port = args.mqtt_port

    brain = BrainService(config)

    def handle_signal(signum, frame):
        log.info("Signal %d received", signum)
        brain.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    brain.start()


if __name__ == "__main__":
    main()
