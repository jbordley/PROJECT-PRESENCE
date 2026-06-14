"""
SENTINEL Three-Layer Validation
=================================
Every sensor reading passes through three validation layers before
entering the world model (Spec Section 3.1).

Layer 1 — Sensor Health Gate:
    Is this sensor physically capable of producing reliable data right now?
    Check operating range against current environmental conditions.

Layer 2 — Physical Plausibility:
    Does this reading make physical sense? Not "is it normal" — is it possible.
    Physics is the filter.

Layer 3 — Cross-Sensor Consistency:
    Readings that passed Layers 1-2 are compared across modalities for
    causal consistency. Not just agreement — causal consistency.

Output: confidence-weighted probability, not a decision.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from sentinel.schemas.messages import SensorReading, SensorHealth

log = logging.getLogger("sentinel.fusion.validation")


# ── Operating Ranges (per sensor type) ────────────────────────────────────
# Each sensor has defined conditions under which it can produce reliable data.

OPERATING_RANGES = {
    "radar": {
        "temp_min_c": -20, "temp_max_c": 60,
        "humidity_max_pct": 95,
        "notes": "Strong RF interference or metallic occlusion degrades",
    },
    "thermal": {
        "temp_min_c": -10, "temp_max_c": 50,
        "notes": "Extreme ambient temp or direct sunlight saturates",
    },
    "lidar": {
        "temp_min_c": -10, "temp_max_c": 50,
        "humidity_max_pct": 90,
        "notes": "Highly reflective surfaces, fog/smoke degrade",
    },
    "camera": {
        "notes": "Darkness mitigated by IR; occlusion is primary failure",
    },
    "acoustic": {
        "notes": "High ambient noise environments degrade SNR",
    },
    "voc": {
        "temp_min_c": -10, "temp_max_c": 60,
        "humidity_max_pct": 80,
        "notes": "Humidity drift requires normalization",
    },
    "barometric": {
        "notes": "HVAC pressure changes filterable via baseline delta",
    },
    "vibration": {
        "notes": "Building-level vibration (trains/trucks) is noise source",
    },
    "emrf": {
        "notes": "Dense RF environments require calibration baseline",
    },
    "csi": {
        "temp_min_c": -20, "temp_max_c": 60,
        "notes": "Metallic objects, large furniture moves invalidate baseline",
    },
}

# Physical limits for plausibility checks
HUMAN_MAX_SPEED_MMS = 10000      # 10 m/s — fastest reasonable human movement indoors
HUMAN_TEMP_MIN_C = 30.0          # Surface temp of a living human
HUMAN_TEMP_MAX_C = 42.0          # Fever range
AMBIENT_TEMP_CHANGE_MAX_C = 5.0  # Max reasonable temp change per minute
PRESSURE_DOOR_EVENT_HPA = 0.3    # Min pressure delta for door/window event


# ── Layer 1: Sensor Health Gate ───────────────────────────────────────────

def check_sensor_health(
    reading: SensorReading,
    env_temp_c: Optional[float] = None,
    env_humidity_pct: Optional[float] = None,
) -> SensorHealth:
    """
    Layer 1: Is this sensor physically capable of producing reliable data?

    Checks the sensor's operating range against current environmental conditions.
    Returns NOMINAL, DEGRADED, or OFFLINE.
    """
    sensor_type = reading.sensor_type
    ranges = OPERATING_RANGES.get(sensor_type, {})

    # If node already reports offline, trust it
    if reading.health == SensorHealth.OFFLINE.value:
        return SensorHealth.OFFLINE

    degraded = False

    # Temperature range check
    if env_temp_c is not None:
        if "temp_min_c" in ranges and env_temp_c < ranges["temp_min_c"]:
            log.debug("%s/%s: temp %.1f below min %.1f — OFFLINE",
                      reading.zone, sensor_type, env_temp_c, ranges["temp_min_c"])
            return SensorHealth.OFFLINE
        if "temp_max_c" in ranges and env_temp_c > ranges["temp_max_c"]:
            log.debug("%s/%s: temp %.1f above max %.1f — OFFLINE",
                      reading.zone, sensor_type, env_temp_c, ranges["temp_max_c"])
            return SensorHealth.OFFLINE
        # Near boundary = degraded
        if "temp_min_c" in ranges and env_temp_c < ranges["temp_min_c"] + 5:
            degraded = True
        if "temp_max_c" in ranges and env_temp_c > ranges["temp_max_c"] - 5:
            degraded = True

    # Humidity check
    if env_humidity_pct is not None:
        if "humidity_max_pct" in ranges and env_humidity_pct > ranges["humidity_max_pct"]:
            log.debug("%s/%s: humidity %.1f%% above max — DEGRADED",
                      reading.zone, sensor_type, env_humidity_pct)
            degraded = True

    # If node reports degraded, trust it
    if reading.health == SensorHealth.DEGRADED.value:
        degraded = True

    return SensorHealth.DEGRADED if degraded else SensorHealth.NOMINAL


# ── Layer 2: Physical Plausibility ────────────────────────────────────────

@dataclass
class PlausibilityResult:
    plausible: bool = True
    notes: str = ""
    adjusted_confidence: float = 1.0  # multiplier applied to reading confidence


def check_plausibility(reading: SensorReading) -> PlausibilityResult:
    """
    Layer 2: Does this reading make physical sense?

    Not "is it normal" — is it physically *possible*.
    Reject impossible values. Flag improbable ones.
    """
    sensor_type = reading.sensor_type
    data = reading.reading

    # ── Radar plausibility ────────────────────────────────────────────
    if sensor_type == "radar":
        targets = data.get("targets", [])
        for t in targets:
            speed = abs(t.get("speed_mms", 0))
            if speed > HUMAN_MAX_SPEED_MMS:
                return PlausibilityResult(
                    plausible=False,
                    notes=f"Radar speed {speed}mm/s exceeds human max {HUMAN_MAX_SPEED_MMS}mm/s",
                    adjusted_confidence=0.0,
                )
            distance = t.get("distance_mm", 0)
            if distance < 0:
                return PlausibilityResult(
                    plausible=False,
                    notes=f"Negative distance {distance}mm — sensor error",
                    adjusted_confidence=0.0,
                )

    # ── Thermal plausibility ──────────────────────────────────────────
    if sensor_type == "thermal":
        hot_spots = data.get("hot_spots", [])
        for spot in hot_spots:
            temp = spot.get("temp_c", 0)
            if temp > 100:
                return PlausibilityResult(
                    plausible=False,
                    notes=f"Thermal spot {temp}C — likely sensor error or fire",
                    adjusted_confidence=0.1,
                )

    # ── Barometric plausibility ───────────────────────────────────────
    if sensor_type == "barometric":
        delta = abs(data.get("delta_hpa", 0))
        if delta > 10:
            return PlausibilityResult(
                plausible=False,
                notes=f"Pressure delta {delta}hPa — physically implausible for indoor event",
                adjusted_confidence=0.0,
            )

    # ── VOC plausibility ──────────────────────────────────────────────
    if sensor_type == "voc":
        # VOC spike without corresponding temperature shift is suspicious
        # (but not impossible — cooking can spike VOC without much temp change)
        # Flag for cross-sensor check rather than rejecting
        pass

    # ── CSI plausibility ──────────────────────────────────────────────
    if sensor_type == "csi":
        variance = data.get("variance", 0)
        if variance > 10000:
            return PlausibilityResult(
                plausible=False,
                notes=f"CSI variance {variance} — likely interference or hardware fault",
                adjusted_confidence=0.1,
            )

    return PlausibilityResult(plausible=True)


# ── Layer 3: Cross-Sensor Consistency ─────────────────────────────────────

@dataclass
class ConsistencyResult:
    """Result of cross-sensor consistency check for a zone."""
    consistent: bool = True
    confidence: float = 0.0
    agreement_sensors: list = field(default_factory=list)
    disagreement_sensors: list = field(default_factory=list)
    notes: str = ""
    # The key insight: disagreement is physically informative
    informative_disagreements: list = field(default_factory=list)


def check_cross_sensor_consistency(
    zone: str,
    readings: dict[str, SensorReading],  # sensor_type → latest reading
) -> ConsistencyResult:
    """
    Layer 3: Compare validated readings across modalities for causal consistency.

    Not just agreement — causal consistency. Radar says present but thermal
    says no heat source? That's not just disagreement — it's physically
    informative (either radar wrong, or something heat-neutral is moving).

    Each sensor independently constrains the possibility space. When enough
    sensors narrow the space to one explanation, that is the world state.
    """
    result = ConsistencyResult()

    # Collect presence votes from each modality
    presence_votes: dict[str, bool] = {}
    motion_votes: dict[str, bool] = {}

    for sensor_type, reading in readings.items():
        data = reading.reading

        if sensor_type == "radar":
            presence_votes["radar"] = data.get("target_count", 0) > 0
            speed = max(
                (abs(t.get("speed_mms", 0)) for t in data.get("targets", [])),
                default=0
            )
            motion_votes["radar"] = speed > 50  # > 50mm/s = moving

        elif sensor_type == "csi":
            presence_votes["csi"] = data.get("present", False)
            motion_votes["csi"] = data.get("motion", False)

        elif sensor_type == "thermal":
            presence_votes["thermal"] = data.get("human_shaped_blobs", 0) > 0

        elif sensor_type == "camera":
            presence_votes["camera"] = data.get("persons_detected", 0) > 0

        elif sensor_type == "acoustic":
            # Acoustic is supporting evidence, not primary
            pass

        elif sensor_type == "vibration":
            event = data.get("event")
            if event == "footstep":
                presence_votes["vibration"] = True
                motion_votes["vibration"] = True

    if not presence_votes:
        return result

    # ── Bayesian-style weighted agreement ─────────────────────────────
    # For now (Stage 1): simple majority with trust weighting
    # Stage 4: full Bayesian with learned per-sensor per-zone reliability

    agree_present = [s for s, v in presence_votes.items() if v]
    agree_absent = [s for s, v in presence_votes.items() if not v]

    total_sensors = len(presence_votes)
    present_fraction = len(agree_present) / total_sensors

    # Simple confidence: fraction of sensors that agree, weighted
    result.confidence = present_fraction if present_fraction > 0.5 else (1.0 - present_fraction)
    result.agreement_sensors = agree_present if present_fraction > 0.5 else agree_absent
    result.disagreement_sensors = agree_absent if present_fraction > 0.5 else agree_present

    # ── Detect physically informative disagreements ───────────────────
    # These are cases where the disagreement itself tells us something

    if "radar" in agree_present and "thermal" in agree_absent:
        result.informative_disagreements.append({
            "type": "radar_present_thermal_absent",
            "meaning": "Moving object without heat signature — possible pet, robot, or draft",
            "zone": zone,
        })

    if "thermal" in agree_present and "radar" in agree_absent and "csi" in agree_absent:
        result.informative_disagreements.append({
            "type": "thermal_present_radar_csi_absent",
            "meaning": "Heat source without motion or WiFi disturbance — stationary warm object or residual heat",
            "zone": zone,
        })

    if "csi" in agree_present and "radar" in agree_absent:
        result.informative_disagreements.append({
            "type": "csi_present_radar_absent",
            "meaning": "WiFi disturbance without radar target — possible person behind metallic obstruction",
            "zone": zone,
        })

    result.consistent = len(result.informative_disagreements) == 0
    if result.informative_disagreements:
        result.notes = "; ".join(d["meaning"] for d in result.informative_disagreements)

    return result
