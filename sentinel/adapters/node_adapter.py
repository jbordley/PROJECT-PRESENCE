#!/usr/bin/env python3
"""
Node-to-Sentinel Adapter
=========================
Bridges ESP32-S3 node MQTT output (home/sentinel/{node_id}/...) into the
sentinel/ topic hierarchy. Runs alongside the nodes — no firmware changes needed.

Subscribes to:
  home/sentinel/+/radar        → sentinel/sensors/{zone}/radar/raw
  home/sentinel/+/devices      → sentinel/sensors/{zone}/emrf/raw
  home/sentinel/+/environment  → sentinel/sensors/{zone}/voc/raw
  home/sentinel/+/acoustic     → sentinel/sensors/{zone}/acoustic/raw
  home/sentinel/+/lidar        → sentinel/sensors/{zone}/lidar/raw
  home/sentinel/+/status       → sentinel/system/{node_id}/health

Also publishes environment context alongside each SensorReading when
a recent BME688 reading is available for that node.

Usage:
  python -m sentinel.adapters.node_adapter [--mqtt-host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from typing import Optional

import paho.mqtt.client as mqtt

from sentinel.config import SentinelConfig, CONFIG_PATH
from sentinel.topics import Sensors, System
from sentinel.schemas.messages import SensorReading, SensorHealth, NodeHealth
from sentinel.intelligence.emrf_intelligence import EmrfIntelligence

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sentinel.adapter.node")


# ── Node topic prefix (matches network.h MQTT_PREFIX + NODE_ID) ──────────
NODE_TOPIC_PREFIX = "home/sentinel/"


class NodeAdapter:
    """Translates home/sentinel/{node_id}/* into sentinel/sensors/{zone}/*/raw."""

    def __init__(self, config: SentinelConfig):
        self.config = config
        self.running = False

        # Node → zone mapping from config
        # Config uses "node_1" format, firmware uses "node-01" format
        self._node_zones: dict[str, str] = {}
        for node_id, node_cfg in config.nodes.items():
            if node_cfg.zone:
                self._node_zones[node_id] = node_cfg.zone
                # Also map alternate ID formats so both work:
                #   node_1 ↔ node-01, node-01 ↔ node_1
                if "_" in node_id:
                    numeric = node_id.split("_", 1)[1]
                    self._node_zones[f"node-{numeric.zfill(2)}"] = node_cfg.zone
                elif "-" in node_id:
                    numeric = node_id.split("-", 1)[1].lstrip("0") or "0"
                    self._node_zones[f"node_{numeric}"] = node_cfg.zone

        # Cache last environment reading per node (for enriching other sensors)
        self._node_env: dict[str, dict] = {}

        # Known device MAC → identity lookup
        self._mac_identity = config.build_mac_identity_map()
        if self._mac_identity:
            log.info("Loaded %d known device MACs for %s",
                     len(self._mac_identity),
                     ", ".join(sorted({v["name"] for v in self._mac_identity.values()})))

        # Known infrastructure MAC → category lookup
        self._infra_identity = config.build_infra_identity_map()
        if self._infra_identity:
            log.info("Loaded %d infrastructure MACs across %s",
                     len(self._infra_identity),
                     ", ".join(sorted({v["category"] for v in self._infra_identity.values()})))

        # BLE name → identity lookup (fallback when MAC randomization defeats MAC matching)
        self._ble_name_identity = config.build_ble_name_identity_map()
        if self._ble_name_identity:
            log.info("Loaded %d BLE name identities for %s",
                     len(self._ble_name_identity),
                     ", ".join(sorted({v["name"] for v in self._ble_name_identity.values()})))

        # EMRF Intelligence Engine — stateful device tracking + analysis
        self._emrf_intel = EmrfIntelligence(
            self._mac_identity, self._infra_identity, self._ble_name_identity
        )
        log.info("EMRF Intelligence Engine initialized")

        # Track message counts for health reporting
        self._msg_counts: dict[str, int] = {}

        # MQTT client
        client_id = f"{config.mqtt.client_id_prefix}-node-adapter"
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

    # ── Zone Resolution ──────────────────────────────────────────────────

    def _resolve_zone(self, node_id: str) -> Optional[str]:
        """Resolve node_id to zone name. Tries multiple ID formats."""
        if node_id in self._node_zones:
            return self._node_zones[node_id]
        # Try normalizing: "node-01" → "node_1", "node-1" → "node_1"
        normalized = node_id.replace("-", "_")
        if normalized in self._node_zones:
            return self._node_zones[normalized]
        # Strip leading zeros: "node_01" → "node_1"
        parts = normalized.split("_", 1)
        if len(parts) == 2:
            stripped = f"{parts[0]}_{parts[1].lstrip('0') or '0'}"
            if stripped in self._node_zones:
                return self._node_zones[stripped]
        return None

    # ── Environment Context ──────────────────────────────────────────────

    def _get_env_context(self, node_id: str) -> dict:
        """Return last known environment from BME688 for this node."""
        env = self._node_env.get(node_id, {})
        if not env:
            return {}
        temp_c = env.get("temp_c")
        temp_f = round(temp_c * 9.0 / 5.0 + 32.0, 1) if temp_c is not None else None
        return {
            "temperature_c": temp_c,
            "temperature_f": temp_f,
            "humidity_pct": env.get("humidity"),
            "pressure_hpa": env.get("pressure_hpa"),
            "gas_resistance_ohms": env.get("gas_ohms"),
        }

    # ── MQTT Callbacks ───────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed: rc=%d", rc)
            return

        log.info("MQTT connected")

        # Subscribe to all node sensor topics
        client.subscribe("home/sentinel/+/radar", qos=0)
        client.subscribe("home/sentinel/+/devices", qos=0)
        client.subscribe("home/sentinel/+/environment", qos=0)
        client.subscribe("home/sentinel/+/acoustic", qos=0)
        client.subscribe("home/sentinel/+/lidar", qos=0)
        client.subscribe("home/sentinel/+/status", qos=0)

        log.info("Subscribed to home/sentinel/+/{radar,devices,environment,acoustic,lidar,status}")
        log.info("Zone mappings: %s", self._node_zones)

    @staticmethod
    def _salvage_json(raw: bytes) -> Optional[dict]:
        """Attempt to parse truncated JSON from firmware buffer overflow.

        Strategy: find the last valid '}' and try parsing up to there.
        If that fails, extract counts with regex as fallback.
        """
        try:
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            return None

        # Strip trailing garbage (null bytes, partial UTF-8, etc.)
        text = text.rstrip('\x00').rstrip()

        # Try full parse first (maybe it's fine after stripping nulls)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Find last '}' and try to close the JSON there
        last_brace = text.rfind('}')
        if last_brace > 0:
            candidate = text[:last_brace + 1]
            # Count unclosed brackets/braces and try to close them
            open_brackets = candidate.count('[') - candidate.count(']')
            open_braces = candidate.count('{') - candidate.count('}')
            candidate += ']' * max(open_brackets, 0)
            candidate += '}' * max(open_braces, 0)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        # Last resort: regex extract counts from partial JSON
        import re
        result = {}
        for key in ("wifi_count", "ble_count", "ts"):
            m = re.search(rf'"{key}"\s*:\s*(\d+)', text)
            if m:
                result[key] = int(m.group(1))
        if result:
            # Provide empty arrays since full device lists were truncated
            result.setdefault("wifi", [])
            result.setdefault("ble", [])
            return result

        return None

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Attempt salvage for truncated payloads (firmware buffer overflow)
            payload = self._salvage_json(msg.payload)
            if payload is None:
                log.warning("Unparseable payload on %s (%d bytes, starts: %s)",
                            msg.topic, len(msg.payload),
                            msg.payload[:80].hex())
                return
            log.info("Salvaged truncated JSON on %s (%d bytes → %d keys)",
                     msg.topic, len(msg.payload), len(payload))

        topic = msg.topic
        parts = topic.split("/")

        # Expected: home/sentinel/{node_id}/{sensor_type}
        if len(parts) != 4 or parts[0] != "home" or parts[1] != "sentinel":
            return

        node_id = parts[2]
        sensor_type = parts[3]

        # Track message counts
        key = f"{node_id}/{sensor_type}"
        self._msg_counts[key] = self._msg_counts.get(key, 0) + 1

        # Route to appropriate handler
        handler = {
            "radar": self._translate_radar,
            "devices": self._translate_devices,
            "environment": self._translate_environment,
            "acoustic": self._translate_acoustic,
            "lidar": self._translate_lidar,
            "status": self._translate_status,
        }.get(sensor_type)

        if handler:
            handler(node_id, payload)

    # ── Sensor Translators ───────────────────────────────────────────────

    def _translate_radar(self, node_id: str, payload: dict):
        """
        Node publishes: {"ts":..., "n":..., "targets":[{"x":..., "y":..., "spd":..., "dist":...}]}
        Sentinel expects: SensorReading with RADAR_READING_SCHEMA
        """
        zone = self._resolve_zone(node_id)
        if not zone:
            log.debug("No zone for %s, skipping radar", node_id)
            return

        targets = []
        for t in payload.get("targets", []):
            targets.append({
                "x_mm": t.get("x", 0),
                "y_mm": t.get("y", 0),
                "speed_mms": t.get("spd", 0) * 10,  # firmware sends cm/s, schema expects mm/s
                "distance_mm": t.get("dist", 0),
            })

        target_count = payload.get("n", len(targets))

        # Confidence: high if targets present (radar is very reliable)
        confidence = 0.9 if target_count > 0 else 0.1

        reading = SensorReading(
            node_id=node_id,
            zone=zone,
            sensor_type="radar",
            reading={
                "targets": targets,
                "target_count": target_count,
                "breathing_detected": False,
                "breathing_bpm": None,
            },
            confidence=round(confidence, 3),
            health=SensorHealth.NOMINAL.value,
            environment=self._get_env_context(node_id),
            physics_plausible=True,
        )

        topic = Sensors.raw(zone, "radar")
        self.mqttc.publish(topic, reading.to_json(), qos=0)

    def _translate_devices(self, node_id: str, payload: dict):
        """
        Node publishes: {"wifi":[{mac,rssi,seen,name?}], "ble":[...], "wifi_count":N, "ble_count":N, "ts":...}

        OLD: counted devices into 3 buckets and called it a day.
        NEW: runs through EmrfIntelligence engine which tracks every device
             across scans, computes RSSI→proximity, detects arrivals/departures,
             identifies vendors, assesses threats, and builds a narrative.
        """
        zone = self._resolve_zone(node_id)
        if not zone:
            log.debug("No zone for %s, skipping devices", node_id)
            return

        wifi_devices = payload.get("wifi", [])
        ble_devices = payload.get("ble", [])

        # ── Run through intelligence engine ───────────────────────────
        intel = self._emrf_intel.process_scan(wifi_devices, ble_devices, zone, node_id)

        # Extract confidence (engine computes a smarter version)
        confidence = intel.pop("_confidence", 0.5)

        # ── Publish enriched EMRF reading ─────────────────────────────
        reading = SensorReading(
            node_id=node_id,
            zone=zone,
            sensor_type="emrf",
            reading=intel,
            confidence=confidence,
            health=SensorHealth.NOMINAL.value,
            environment=self._get_env_context(node_id),
            physics_plausible=True,
        )

        topic = Sensors.raw(zone, "emrf")
        self.mqttc.publish(topic, reading.to_json(), qos=0)

        # ── Publish individual events for brain/agent ─────────────────
        events = intel.get("events", [])
        for event in events:
            event_type = event.get("event_type", "unknown")
            event_topic = f"sentinel/events/{zone}/emrf/{event_type}"
            self.mqttc.publish(event_topic, json.dumps(event), qos=0)
            if event_type in ("arrival", "departure", "new_device"):
                log.info("EMRF event [%s/%s]: %s",
                         zone, event_type, event.get("description", ""))

        # ── Log intelligence narrative periodically ───────────────────
        narrative = intel.get("intelligence", "")
        if narrative and self._msg_counts.get(f"{node_id}/devices", 0) % 10 == 0:
            log.info("EMRF intelligence [%s]: %s", zone, narrative)

    def _translate_environment(self, node_id: str, payload: dict):
        """
        Node publishes: {"temp_c":..., "humidity":..., "pressure_hpa":..., "gas_ohms":..., "pressure_delta":..., "ts":...}
        Sentinel expects: SensorReading with voc sensor type
        """
        zone = self._resolve_zone(node_id)
        if not zone:
            log.debug("No zone for %s, skipping environment", node_id)
            return

        # Cache for enriching other sensors
        self._node_env[node_id] = payload

        gas_ohms = payload.get("gas_ohms", 0)
        humidity = payload.get("humidity")
        temp_c = payload.get("temp_c")
        pressure_delta = payload.get("pressure_delta", 0.0)

        # Temperature in both units
        temp_f = round(temp_c * 9.0 / 5.0 + 32.0, 1) if temp_c is not None else None

        # ── BME688 warmup detection ──────────────────────────────────
        # After power cycle, gas_ohms=0 and humidity=100% until the MOX
        # heater stabilizes (~10min). Mark as DEGRADED so fusion doesn't
        # treat garbage readings as real data.
        warming_up = (gas_ohms == 0) or (humidity is not None and humidity >= 99.9)
        health = SensorHealth.DEGRADED.value if warming_up else SensorHealth.NOMINAL.value
        physics_plausible = not warming_up

        # VOC/gas can indicate human presence (CO2 proxy via gas resistance drop)
        # Lower gas resistance = more VOC = more likely human present
        # This is slow and only supporting evidence
        confidence = 0.05 if warming_up else 0.3

        # Pressure delta can indicate door/window events
        physics_notes = ""
        if warming_up:
            physics_notes = "BME688 warming up — gas/humidity unreliable"
        elif abs(pressure_delta) > 0.5:
            physics_notes = f"pressure_delta={pressure_delta:.2f}hPa (possible door/window event)"

        reading = SensorReading(
            node_id=node_id,
            zone=zone,
            sensor_type="voc",
            reading={
                "temperature_c": temp_c,
                "temperature_f": temp_f,
                "humidity_pct": humidity,
                "pressure_hpa": payload.get("pressure_hpa"),
                "gas_resistance_ohms": gas_ohms,
                "pressure_delta_hpa": pressure_delta,
                "warming_up": warming_up,
            },
            confidence=round(confidence, 3),
            health=health,
            environment={},  # This IS the environment sensor
            physics_plausible=physics_plausible,
            physics_notes=physics_notes,
        )

        topic = Sensors.raw(zone, "voc")
        self.mqttc.publish(topic, reading.to_json(), qos=0)

    def _translate_acoustic(self, node_id: str, payload: dict):
        """
        Node publishes: {"rms_db":..., "peak_db":..., "ambient_db":..., "impulsive":bool, "ts":...}
        Sentinel expects: SensorReading with acoustic sensor type
        """
        zone = self._resolve_zone(node_id)
        if not zone:
            log.debug("No zone for %s, skipping acoustic", node_id)
            return

        rms_db = payload.get("rms_db", 0.0)
        ambient_db = payload.get("ambient_db", 0.0)
        impulsive = payload.get("impulsive", False)

        # Acoustic is supporting evidence — significant sound above ambient suggests presence
        delta = rms_db - ambient_db if ambient_db else 0
        confidence = min(0.2 + max(delta, 0) / 30.0 * 0.3, 0.5)
        if impulsive:
            confidence = min(confidence + 0.15, 0.55)

        reading = SensorReading(
            node_id=node_id,
            zone=zone,
            sensor_type="acoustic",
            reading={
                "rms_db": rms_db,
                "peak_db": payload.get("peak_db", 0.0),
                "ambient_db": ambient_db,
                "impulsive": impulsive,
                "delta_db": round(delta, 1),
            },
            confidence=round(confidence, 3),
            health=SensorHealth.NOMINAL.value,
            environment=self._get_env_context(node_id),
            physics_plausible=True,
        )

        topic = Sensors.raw(zone, "acoustic")
        self.mqttc.publish(topic, reading.to_json(), qos=0)

    def _translate_lidar(self, node_id: str, payload: dict):
        """
        Node publishes: {"ts":..., "scan":..., "points":N, "zones":[{"sector":N, "min_mm":N, "hits":N}]}
        Sentinel expects: SensorReading with lidar sensor type
        """
        zone = self._resolve_zone(node_id)
        if not zone:
            log.debug("No zone for %s, skipping lidar", node_id)
            return

        sectors = payload.get("zones", [])
        point_count = payload.get("points", 0)

        # Lidar detects objects by short-range readings in sectors
        # Count sectors with close-range hits (< 3m) as presence indicators
        close_sectors = sum(
            1 for s in sectors
            if 0 < s.get("min_mm", 0) < 3000 and s.get("hits", 0) > 2
        )

        confidence = min(0.5 + close_sectors / 12.0 * 0.4, 0.85) if close_sectors > 0 else 0.1

        reading = SensorReading(
            node_id=node_id,
            zone=zone,
            sensor_type="lidar",
            reading={
                "scan_number": payload.get("scan", 0),
                "point_count": point_count,
                "sectors": sectors,
                "close_sectors": close_sectors,
            },
            confidence=round(confidence, 3),
            health=SensorHealth.NOMINAL.value,
            environment=self._get_env_context(node_id),
            physics_plausible=True,
        )

        topic = Sensors.raw(zone, "lidar")
        self.mqttc.publish(topic, reading.to_json(), qos=0)

    def _translate_status(self, node_id: str, payload: dict):
        """
        Node publishes heartbeat: {"uptime":..., "heap":..., "ip":..., "sensors":{...}}
        Republish as sentinel/system/{node_id}/health
        """
        zone = self._resolve_zone(node_id) or "unknown"

        # Build per-sensor health from heartbeat
        sensor_status = payload.get("sensors", {})
        sensor_health = {}
        for sensor, enabled in sensor_status.items():
            if isinstance(enabled, bool):
                sensor_health[sensor] = (
                    SensorHealth.NOMINAL.value if enabled
                    else SensorHealth.OFFLINE.value
                )

        health = NodeHealth(
            node_id=node_id,
            zone=zone,
            uptime_sec=payload.get("uptime_s", 0),
            sensor_health=sensor_health,
            free_heap_bytes=payload.get("heap_free"),
            wifi_rssi=payload.get("wifi_rssi"),
        )

        # Also include environment if cached
        env = self._node_env.get(node_id, {})
        if env:
            health.temperature_c = env.get("temp_c")
            health.humidity_pct = env.get("humidity")

        topic = System.health(node_id)
        self.mqttc.publish(topic, health.to_json(), qos=0, retain=True)

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
        log.info("Node adapter started")

        try:
            while self.running:
                time.sleep(5)
                # Periodic stats log
                if self._msg_counts:
                    total = sum(self._msg_counts.values())
                    log.debug("Messages translated: %d total (%s)", total, dict(self._msg_counts))
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if not self.running:
            return
        self.running = False
        log.info("Node adapter stopping...")
        total = sum(self._msg_counts.values())
        log.info("Final stats: %d messages translated", total)
        self.mqttc.loop_stop()
        self.mqttc.disconnect()
        log.info("Node adapter stopped")


def main():
    parser = argparse.ArgumentParser(description="SENTINEL Node Adapter")
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

    adapter = NodeAdapter(config)

    def handle_signal(signum, frame):
        adapter.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    adapter.start()


if __name__ == "__main__":
    main()
