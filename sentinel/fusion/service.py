#!/usr/bin/env python3
"""
SENTINEL Fusion Service
========================
Stage 1-2: Subscribes to all raw sensor topics per zone, applies
three-layer validation, and produces interpreted zone state.

Data flow:
  sentinel/sensors/{zone}/{sensor_type}/raw
    → Layer 1: Sensor Health Gate
    → Layer 2: Physical Plausibility
    → Layer 3: Cross-Sensor Consistency
    → Weighted fusion → ZoneOccupancy
    → Publish to sentinel/context/{zone}/occupancy

The fusion service is lightweight and stateless per-cycle.
It maintains a sliding window of recent readings per sensor per zone
for temporal consistency, but does NOT maintain narrative state —
that's the brain's job.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import paho.mqtt.client as mqtt

from pathlib import Path

from sentinel.config import SentinelConfig, CONFIG_PATH
from sentinel.topics import Sensors, Context, System
from sentinel.schemas.messages import (
    SensorReading,
    SensorHealth,
    ZoneOccupancy,
    OccupancyState,
)
from sentinel.fusion.validation import (
    check_sensor_health,
    check_plausibility,
    check_cross_sensor_consistency,
    ConsistencyResult,
)
from sentinel.geometry import (
    trilaterate,
    find_zone,
    rssi_to_distance_ft,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sentinel.fusion")


# ── Sensor Trust Weights (Stage 1: static, Stage 4: learned) ─────────────
# Higher = more trusted for presence detection in ideal conditions.

DEFAULT_TRUST_WEIGHTS = {
    "radar": 0.9,       # Very reliable for presence + motion
    "thermal": 0.85,    # Excellent for presence + liveness
    "csi": 0.7,         # Good when calibrated, noisy otherwise
    "camera": 0.8,      # Good for identity, less for presence alone
    "lidar": 0.75,      # Good for geometry + body detection
    "acoustic": 0.4,    # Supporting evidence only
    "vibration": 0.3,   # Supporting evidence only
    "barometric": 0.2,  # Event detection, not presence
    "voc": 0.3,         # Metabolic detection, slow response
    "emrf": 0.4,        # Device activity detection
}


@dataclass
class ZoneTracker:
    """Tracks recent sensor readings and state for one zone."""
    zone: str = ""
    latest_readings: dict = field(default_factory=dict)  # sensor_type → SensorReading
    last_environment: dict = field(default_factory=dict)  # temp, humidity, etc.
    last_occupancy: Optional[ZoneOccupancy] = None
    last_publish_time: float = 0.0


class FusionService:
    """
    Per-zone sensor fusion. Subscribes to raw sensor data,
    validates through three layers, produces zone occupancy.
    """

    def __init__(self, config: SentinelConfig):
        self.config = config
        self.running = False
        self._lock = threading.Lock()

        # Per-zone tracking
        self._zones: dict[str, ZoneTracker] = {}
        for zone_name in config.zones:
            self._zones[zone_name] = ZoneTracker(zone=zone_name)

        # Polygon zones + node positions for trilateration
        self._zone_polygons = config.zone_polygons()   # {zone_id: [[x,y], ...]}
        self._node_positions = config.node_positions()  # {node_id: (x, y)}
        log.info("Geometry: %d zone polygons, %d node positions",
                 len(self._zone_polygons), len(self._node_positions))

        # Cross-node EMRF cache: {mac: {node_id: (rssi, timestamp)}}
        # Updated each time an EMRF reading arrives from a node.
        # Used for multi-node trilateration of device positions.
        self._emrf_node_cache: dict[str, dict[str, tuple[int, float]]] = {}
        _EMRF_CACHE_TTL = 60.0  # seconds before a cached reading is stale
        self._emrf_cache_ttl = _EMRF_CACHE_TTL

        # Load room config for LiDAR sector-aware presence detection
        self._room_config = None
        room_config_path = Path(__file__).parent.parent / "room_config.json"
        if room_config_path.exists():
            try:
                with open(room_config_path) as f:
                    self._room_config = json.load(f)
                log.info("Loaded room config from %s", room_config_path)
            except Exception as e:
                log.warning("Failed to load room_config.json: %s", e)

        # MQTT
        client_id = f"{config.mqtt.client_id_prefix}-fusion"
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
        self.mqttc.on_disconnect = self._on_disconnect

        if config.mqtt.username:
            self.mqttc.username_pw_set(config.mqtt.username, config.mqtt.password)

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed: rc=%d", rc)
            return

        log.info("MQTT connected")

        # Subscribe to all raw sensor data
        client.subscribe(Sensors.raw_wildcard_all(), qos=1)
        log.info("Subscribed: %s", Sensors.raw_wildcard_all())

        log.info("Fusion service online — tracking %d zones", len(self._zones))

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.warning("MQTT disconnected: rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        topic = msg.topic

        # Parse topic: sentinel/sensors/{zone}/{sensor_type}/raw
        parts = topic.split("/")
        if len(parts) < 5 or parts[0] != "sentinel" or parts[1] != "sensors":
            return

        zone = parts[2]
        sensor_type = parts[3]

        try:
            reading = SensorReading.from_dict(payload)
        except Exception as e:
            log.debug("Failed to parse reading: %s", e)
            return

        # Fill in from topic if missing
        if not reading.zone:
            reading.zone = zone
        if not reading.sensor_type:
            reading.sensor_type = sensor_type

        with self._lock:
            self._process_reading(reading)

    def _process_reading(self, reading: SensorReading):
        """
        Full three-layer validation pipeline for one sensor reading.
        After validation, triggers zone-level fusion if enough data.
        """
        zone = reading.zone
        sensor_type = reading.sensor_type

        # Get or create zone tracker
        if zone not in self._zones:
            self._zones[zone] = ZoneTracker(zone=zone)
            log.info("New zone discovered: %s", zone)
        tracker = self._zones[zone]

        # Extract environment from reading (if available)
        env = reading.environment or {}
        env_temp = env.get("temperature_c") or tracker.last_environment.get("temperature_c")
        env_humidity = env.get("humidity_pct") or tracker.last_environment.get("humidity_pct")

        # ── Layer 1: Sensor Health Gate ───────────────────────────────────
        health = check_sensor_health(reading, env_temp, env_humidity)

        if health == SensorHealth.OFFLINE:
            log.debug("%s/%s: OFFLINE — excluded from fusion", zone, sensor_type)
            return  # Sensor doesn't participate in fusion

        # ── Layer 2: Physical Plausibility ────────────────────────────────
        plausibility = check_plausibility(reading)

        if not plausibility.plausible and plausibility.adjusted_confidence == 0.0:
            log.warning("%s/%s: REJECTED — %s", zone, sensor_type, plausibility.notes)
            return  # Physically impossible reading

        # Adjust confidence based on health and plausibility
        trust_weight = DEFAULT_TRUST_WEIGHTS.get(sensor_type, 0.5)
        health_multiplier = 1.0 if health == SensorHealth.NOMINAL else 0.6
        final_confidence = (
            reading.confidence
            * trust_weight
            * health_multiplier
            * plausibility.adjusted_confidence
        )
        reading.confidence = round(final_confidence, 3)

        # Store validated reading
        tracker.latest_readings[sensor_type] = reading

        # Cache per-device RSSI for cross-node trilateration
        if sensor_type == "emrf" and reading.node_id:
            self._update_emrf_cache(reading)

        # ── Layer 3: Cross-Sensor Consistency + Fusion ────────────────────
        self._fuse_zone(tracker)

    # ── Cross-Node EMRF Trilateration ────────────────────────────────────

    def _update_emrf_cache(self, reading: SensorReading):
        """Cache per-device RSSI from this node's EMRF reading for trilateration."""
        node_id = reading.node_id
        now = reading.timestamp or time.time()
        data = reading.reading or {}

        # Extract RSSI from persons → devices
        for pid, person_info in data.get("persons", {}).items():
            for dev in person_info.get("devices", []):
                mac = dev.get("mac", "").upper()
                rssi = dev.get("rssi")
                if mac and rssi is not None:
                    self._emrf_node_cache.setdefault(mac, {})[node_id] = (rssi, now)

        # Also cache from unknowns (they may have RSSI too)
        for unk in data.get("unknowns", []):
            mac = unk.get("mac", "").upper()
            rssi = unk.get("rssi")
            if mac and rssi is not None:
                self._emrf_node_cache.setdefault(mac, {})[node_id] = (rssi, now)

        # Prune stale entries
        cutoff = now - self._emrf_cache_ttl
        stale_macs = []
        for mac, nodes in self._emrf_node_cache.items():
            nodes_to_remove = [nid for nid, (_, ts) in nodes.items() if ts < cutoff]
            for nid in nodes_to_remove:
                del nodes[nid]
            if not nodes:
                stale_macs.append(mac)
        for mac in stale_macs:
            del self._emrf_node_cache[mac]

    def _trilaterate_zone(self, mac: str) -> tuple[str | None, tuple[float, float] | None]:
        """Trilaterate a device's position from cached multi-node RSSI readings.

        Returns:
            (zone_id, (x, y)) if trilateration + polygon match succeeded.
            (None, None) if insufficient data or no zone match.
        """
        node_readings = self._emrf_node_cache.get(mac, {})
        if len(node_readings) < 2:
            return None, None

        now = time.time()
        cutoff = now - self._emrf_cache_ttl
        observations = []

        for node_id, (rssi, ts) in node_readings.items():
            if ts < cutoff:
                continue
            pos = self._node_positions.get(node_id)
            if pos is None or len(pos) != 2:
                continue
            dist_ft = rssi_to_distance_ft(rssi)
            if dist_ft == float('inf'):
                continue
            observations.append((pos[0], pos[1], dist_ft))

        if len(observations) < 2:
            return None, None

        position = trilaterate(observations)
        if position is None:
            return None, None

        zone_id = find_zone(position[0], position[1], self._zone_polygons)
        log.debug("Trilateration: MAC=%s → pos=(%.1f, %.1f)ft → zone=%s",
                  mac[-8:], position[0], position[1], zone_id or "unknown")
        return zone_id, position

    def _fuse_zone(self, tracker: ZoneTracker):
        """
        Fuse all validated readings for a zone into a ZoneOccupancy message.
        Rate-limited to avoid flooding.
        """
        now = time.time()
        if now - tracker.last_publish_time < 0.5:  # max 2Hz publish rate
            return

        # Filter out stale readings. Most sensors publish at ~1Hz so 10s
        # is generous. EMRF used to have a 45s window but that caused
        # phantom occupancy from departed devices lingering in the cache.
        STALE_THRESHOLDS = {"emrf": 10.0}
        DEFAULT_STALE = 10.0
        fresh_readings = {
            st: r for st, r in tracker.latest_readings.items()
            if now - r.timestamp < STALE_THRESHOLDS.get(st, DEFAULT_STALE)
        }

        if not fresh_readings:
            # No fresh data — publish absent if we previously published occupied
            if tracker.last_occupancy and tracker.last_occupancy.occupied:
                occ = ZoneOccupancy(
                    zone=tracker.zone,
                    occupied=False,
                    occupant_count=0,
                    confidence=0.5,
                    contributing_sensors=[],
                )
                self._publish_occupancy(tracker, occ)
            return

        # Cross-sensor consistency check
        consistency = check_cross_sensor_consistency(tracker.zone, fresh_readings)

        # Determine presence from weighted fusion
        presence_score = 0.0
        total_weight = 0.0
        contributing = []
        dissenting = []

        for sensor_type, reading in fresh_readings.items():
            data = reading.reading
            weight = reading.confidence  # already health/plausibility adjusted

            # Extract presence signal
            is_present = False
            if sensor_type == "csi":
                is_present = data.get("present", False)
            elif sensor_type == "radar":
                is_present = data.get("target_count", 0) > 0
            elif sensor_type == "thermal":
                is_present = data.get("human_shaped_blobs", 0) > 0
            elif sensor_type == "camera":
                is_present = data.get("persons_detected", 0) > 0
            elif sensor_type == "vibration":
                is_present = data.get("event") == "footstep"
            elif sensor_type == "lidar":
                is_present = data.get("close_sectors", 0) > 0
            else:
                continue  # skip sensors without clear presence signal

            if is_present:
                presence_score += weight
                contributing.append(sensor_type)
            else:
                dissenting.append(sensor_type)
            total_weight += weight

        # Fusion decision
        if total_weight == 0:
            return

        fusion_confidence = presence_score / total_weight
        occupied = fusion_confidence > 0.5  # threshold: >50% weighted evidence

        # Estimate occupant count via weighted consensus.
        # Radar is great for detecting *presence* but unreliable for *count*:
        # the LD2450 splits/merges targets on still people and picks up pets.
        # Thermal blob count and camera person count are much better anchors
        # for how many *humans* are actually in the room.
        #
        # Strategy: collect (count, weight) votes from each sensor that
        # reports a count. Thermal and camera are weighted heavily; radar
        # is down-weighted for counting. The final count is the weighted
        # median — the lowest count whose cumulative weight exceeds 50%.
        occupant_count = 0
        if occupied:
            COUNT_WEIGHTS = {
                "camera":  3.0,   # best — actually identifies humans
                "thermal": 2.5,   # excellent — blob must be human-temp + human-sized
                "lidar":   1.5,   # decent — geometry-aware
                "radar":   0.5,   # presence yes, count unreliable (pets, split targets)
            }

            count_votes = []  # list of (count, weight)
            for sensor_type, reading in fresh_readings.items():
                data = reading.reading
                w = COUNT_WEIGHTS.get(sensor_type)
                if w is None:
                    continue

                if sensor_type == "radar":
                    # Filter radar targets: ignore targets likely too small
                    # to be human (< 300mm distance — sensor noise / pets
                    # right at the sensor). Keep the raw target_count but
                    # trust it less via the low weight above.
                    count = data.get("target_count", 0)
                elif sensor_type == "thermal":
                    count = data.get("human_shaped_blobs", 0)
                elif sensor_type == "camera":
                    count = data.get("persons_detected", 0)
                elif sensor_type == "lidar":
                    count = data.get("close_sectors", 0)
                    # LiDAR sectors != people, but >0 confirms at least 1
                    count = min(count, 2)
                else:
                    continue

                if count > 0:
                    count_votes.append((count, w))

            if count_votes:
                # Weighted median: sort by count, walk cumulative weight
                count_votes.sort(key=lambda x: x[0])
                total_w = sum(w for _, w in count_votes)
                cumulative = 0.0
                for cnt, w in count_votes:
                    cumulative += w
                    if cumulative >= total_w * 0.5:
                        occupant_count = cnt
                        break

            # If sensors voted but produced count=0, trust the occupied
            # decision — at least one person triggered the threshold.
            if occupant_count == 0:
                occupant_count = 1

        # If not occupied, force count to 0 regardless of sensor votes
        if not occupied:
            occupant_count = 0

        # ── Identity resolution from EMRF ──────────────────────────────
        # Two strategies for determining if a person is in THIS zone:
        #
        # 1. TRILATERATION (preferred): If 2+ nodes see a person's device,
        #    estimate (x,y) position and check which polygon zone it falls in.
        #    Much more accurate — eliminates wall-bleed false positives.
        #
        # 2. PROXIMITY FALLBACK: If only 1 node sees the device, use the
        #    original RSSI proximity tiers (immediate/near/room ≤ 5m).
        #
        ZONE_PROXIMITIES = {"immediate", "near", "room"}
        identified_names = []
        emrf_reading = fresh_readings.get("emrf")
        if emrf_reading and emrf_reading.reading:
            emrf_data = emrf_reading.reading
            persons_data = emrf_data.get("persons", {})
            for pid, person_info in persons_data.items():
                name = person_info.get("name", "")
                status = person_info.get("status", "")
                if not name or status in ("departed",):
                    continue
                if name.lower() in identified_names:
                    continue

                # Collect MACs for this person to attempt trilateration
                devices = person_info.get("devices", [])
                trilat_zone = None
                trilat_pos = None
                for dev in devices:
                    mac = dev.get("mac", "").upper()
                    if mac:
                        tz, tp = self._trilaterate_zone(mac)
                        if tz is not None:
                            trilat_zone = tz
                            trilat_pos = tp
                            break  # first successful trilateration wins

                if trilat_zone is not None:
                    # Trilateration succeeded — use polygon zone assignment
                    in_this_zone = (trilat_zone == tracker.zone)
                    log.debug("EMRF trilat: %s → zone=%s, pos=%s → %s for %s",
                              name, trilat_zone, trilat_pos,
                              "INCLUDED" if in_this_zone else "EXCLUDED",
                              tracker.zone)
                    if in_this_zone:
                        identified_names.append(name.lower())
                else:
                    # Fallback: single-node proximity check (original behavior)
                    proximity = person_info.get("closest_proximity", "far")
                    est_distance = person_info.get("estimated_distance_m", -1)
                    in_zone = proximity in ZONE_PROXIMITIES
                    log.debug("EMRF proximity: %s — proximity=%s, distance=%.1fm, status=%s → %s",
                              name, proximity, est_distance, status,
                              "INCLUDED" if in_zone else "EXCLUDED")
                    if in_zone:
                        identified_names.append(name.lower())

        # ── Identity hints from LiDAR seat sectors ─────────────────────
        # LiDAR seat detection requires corroboration from thermal or camera
        # to confirm a living person (not a bag or chair pushed in).
        # Thermal: human_shaped_blobs > 0  |  Camera: persons_detected > 0
        lidar_reading = fresh_readings.get("lidar")
        if lidar_reading and lidar_reading.reading and self._room_config:
            # Check if thermal or camera confirms a living body in the zone
            thermal_reading = fresh_readings.get("thermal")
            camera_reading = fresh_readings.get("camera")
            thermal_confirms = (
                thermal_reading and thermal_reading.reading
                and thermal_reading.reading.get("human_shaped_blobs", 0) > 0
            )
            camera_confirms = (
                camera_reading and camera_reading.reading
                and camera_reading.reading.get("persons_detected", 0) > 0
            )
            liveness_confirmed = thermal_confirms and camera_confirms

            if liveness_confirmed:
                rules = self._room_config.get("detection_rules", {})
                seated = rules.get("seated_detection", {})
                lidar_sectors = lidar_reading.reading.get("sectors", [])

                for person_name, seat_info in seated.items():
                    if person_name.startswith("_"):
                        continue  # skip _doc keys
                    sector_id = seat_info.get("sector", "")  # e.g. "S0"
                    occ_range = seat_info.get("occupied_range_mm", [0, 0])

                    # Find this sector in the LiDAR scan
                    for s in lidar_sectors:
                        s_id = f"S{s.get('sector', -1)}"
                        if s_id == sector_id:
                            min_mm = s.get("min_mm", 0)
                            # Seat is occupied if min_mm is within the occupied range
                            if occ_range[0] <= min_mm <= occ_range[1]:
                                if person_name.lower() not in identified_names:
                                    identified_names.append(person_name.lower())
                                    log.debug("LiDAR+%s confirms %s at %s (min_mm=%d)",
                                              "thermal" if thermal_confirms else "camera",
                                              person_name, sector_id, min_mm)
                            break

        # ── Reconcile count with identity ─────────────────────────────
        # EMRF unknown devices are wireless signals, NOT physical bodies.
        # They should never inflate the physical occupant count. Only
        # thermal/camera/radar vote on how many bodies are in the room.
        # EMRF's role is identity (who), not counting (how many).
        #
        # Cap strategy: if EMRF identifies N people, and no physical sensor
        # (thermal/camera) sees more than N bodies, cap at N.
        if identified_names and emrf_reading:
            # Physical-sensor body count (thermal + camera only)
            physical_max = 0
            thermal_r = fresh_readings.get("thermal")
            camera_r = fresh_readings.get("camera")
            if thermal_r and thermal_r.reading:
                physical_max = max(physical_max, thermal_r.reading.get("human_shaped_blobs", 0))
            if camera_r and camera_r.reading:
                physical_max = max(physical_max, camera_r.reading.get("persons_detected", 0))

            # If camera sees nobody extra beyond EMRF-identified people,
            # trust EMRF identity count over noisy thermal blob count.
            # Thermal blobs often include warm objects (monitors, laptops).
            camera_count = 0
            if camera_r and camera_r.reading:
                camera_count = camera_r.reading.get("persons_detected", 0)
            if camera_count <= len(identified_names):
                # Camera agrees with EMRF — no extra unidentified bodies
                identity_cap = len(identified_names)
            else:
                # Camera sees more people than EMRF knows — trust camera
                identity_cap = max(len(identified_names), camera_count)
            if occupant_count > identity_cap:
                log.debug("Count capped: %d → %d (EMRF identifies %d, physical sensors see %d)",
                          occupant_count, identity_cap, len(identified_names), physical_max)
                occupant_count = identity_cap if occupied else 0

        # Build occupant list: use identified names first, fill remainder with unknown
        occupants = list(identified_names[:occupant_count])
        for i in range(len(occupants), occupant_count):
            occupants.append(f"unknown_{i}")

        occ = ZoneOccupancy(
            zone=tracker.zone,
            occupied=occupied,
            occupant_count=occupant_count,
            occupants=occupants,
            confidence=round(fusion_confidence, 3),
            contributing_sensors=contributing,
            dissenting_sensors=dissenting + [
                d["type"] for d in consistency.informative_disagreements
            ],
        )

        self._publish_occupancy(tracker, occ)

    def _publish_occupancy(self, tracker: ZoneTracker, occ: ZoneOccupancy):
        """Publish zone occupancy to context layer."""
        tracker.last_occupancy = occ
        tracker.last_publish_time = time.time()

        self.mqttc.publish(
            Context.occupancy(tracker.zone),
            occ.to_json(),
            qos=0,
            retain=True,
        )
        log.debug("Zone %s: occupied=%s count=%d conf=%.2f sensors=%s",
                   tracker.zone, occ.occupied, occ.occupant_count,
                   occ.confidence, occ.contributing_sensors)

    def _heartbeat_zones(self):
        """Re-publish last occupancy for zones that haven't updated in 8s.
        Keeps brain's fusion freshness timer alive during quiet periods."""
        now = time.time()
        with self._lock:
            for zone, tracker in self._zones.items():
                if tracker.last_occupancy and (now - tracker.last_publish_time) > 8.0:
                    # Refresh the timestamp so brain knows fusion is still alive
                    tracker.last_occupancy.timestamp = now
                    self._publish_occupancy(tracker, tracker.last_occupancy)
                    log.debug("Heartbeat for zone %s", zone)

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
        log.info("Fusion service started")

        try:
            while self.running:
                time.sleep(1)
                # Heartbeat: re-publish last occupancy for zones that haven't
                # had fresh sensor data recently. This keeps the brain's fusion
                # freshness timer alive so the raw sensor path doesn't create
                # spurious unknowns during quiet periods.
                self._heartbeat_zones()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if not self.running:
            return
        self.running = False
        log.info("Fusion service stopping...")
        self.mqttc.loop_stop()
        self.mqttc.disconnect()
        log.info("Fusion service stopped")


def main():
    parser = argparse.ArgumentParser(description="SENTINEL Fusion Service")
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

    svc = FusionService(config)

    def handle_signal(signum, frame):
        svc.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    svc.start()


if __name__ == "__main__":
    main()
