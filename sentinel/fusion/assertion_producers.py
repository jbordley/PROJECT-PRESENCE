"""
SENTINEL Assertion Producers
==============================
Convert raw sensor intelligence output into SensorAssertions for the
correlation engine.

Each sensor has a producer function that takes the sensor's native output
format and returns a list of SensorAssertions. This is the bridge between
sensor-specific intelligence engines and the unified assertion schema.

Producer functions are stateless — all state lives in the intelligence
engines (emrf_intelligence.py, etc.) and the correlation engine.

Usage:
    from sentinel.fusion.assertion_producers import emrf_to_assertions
    assertions = emrf_to_assertions(emrf_result, zone="office", node_id="node_1")
    engine.ingest_batch(assertions)
"""

from __future__ import annotations

import logging
from typing import Optional

from sentinel.schemas.assertions import (
    SensorAssertion,
    AssertionType,
    SensorSource,
    EntityType,
    SpatialRef,
)

log = logging.getLogger("sentinel.fusion.assertion_producers")


# ── EMRF (WiFi/BLE) Producer ────────────────────────────────────────────

def emrf_to_assertions(emrf_result: dict, zone: str,
                        node_id: str = "") -> list[SensorAssertion]:
    """Convert EMRF intelligence engine output to SensorAssertions.

    The EMRF engine already does heavy lifting (identity matching, cross-
    confirmation, threat assessment). We translate its rich output into
    the assertion schema so the correlation engine can match it against
    other sensors.

    Args:
        emrf_result: Output from EmrfIntelligence.process_scan()
        zone: Zone name
        node_id: Node ID that produced this scan

    Returns:
        List of SensorAssertions (typically 1 per detected entity)
    """
    assertions = []
    now_ts = emrf_result.get("timestamp", 0)

    # ── Known persons → identity + presence assertions ──
    for person_id, person_data in emrf_result.get("persons", {}).items():
        name = person_data.get("name", person_id)
        best_device = max(
            person_data.get("devices", []),
            key=lambda d: d.get("cross_confidence", 0),
            default={}
        )

        # Spatial from best device
        spatial = SpatialRef(
            distance_m=person_data.get("closest_distance_m"),
            proximity=person_data.get("closest_proximity"),
            accuracy_m=2.0,  # RSSI-based distance is rough
        )

        # Identity assertion
        assertions.append(SensorAssertion(
            assertion_type=AssertionType.IDENTITY,
            entity_type=EntityType.PERSON,
            source=SensorSource.EMRF,
            node_id=node_id,
            zone=zone,
            confidence=best_device.get("cross_confidence", 0.5),
            person_id=person_id,
            person_name=name,
            device_mac=best_device.get("mac"),
            device_label=best_device.get("label"),
            spatial=spatial,
            duration_sec=person_data.get("duration_sec", 0),
            evidence={
                "device_count": person_data.get("count", 0),
                "signal_sources": best_device.get("signal_sources", []),
                "rssi": best_device.get("rssi"),
                "status": person_data.get("status"),
                "zone_confidence": person_data.get("zone_confidence"),
            },
        ))

        # Also emit a presence assertion (so correlation rules match)
        assertions.append(SensorAssertion(
            assertion_type=AssertionType.PRESENCE,
            entity_type=EntityType.PERSON,
            source=SensorSource.EMRF,
            node_id=node_id,
            zone=zone,
            confidence=person_data.get("zone_confidence", 0.5),
            person_id=person_id,
            person_name=name,
            spatial=spatial,
            duration_sec=person_data.get("duration_sec", 0),
        ))

    # ── Unknown devices → presence assertions ──
    for unknown in emrf_result.get("unknowns", []):
        spatial = SpatialRef(
            distance_m=unknown.get("distance_m"),
            proximity=unknown.get("proximity"),
            accuracy_m=3.0,  # Unknown devices have less reliable distance
        )

        assertions.append(SensorAssertion(
            assertion_type=AssertionType.PRESENCE,
            entity_type=EntityType.UNKNOWN_DEVICE,
            source=SensorSource.EMRF,
            node_id=node_id,
            zone=zone,
            confidence=unknown.get("cross_confidence", 0.3),
            device_mac=unknown.get("mac"),
            spatial=spatial,
            duration_sec=unknown.get("session_duration", 0),
            evidence={
                "vendor": unknown.get("vendor"),
                "rssi": unknown.get("rssi"),
                "signal_sources": unknown.get("signal_sources", []),
                "cross_classification": unknown.get("cross_classification"),
                "threat_level": unknown.get("threat_level"),
                "scan_count": unknown.get("scan_count", 0),
            },
        ))

    return assertions


# ── CSI Producer ─────────────────────────────────────────────────────────

def csi_to_assertions(csi_data: dict, zone: str,
                       node_id: str = "") -> list[SensorAssertion]:
    """Convert CSI fusion output to SensorAssertions.

    CSI provides zone-level presence/motion/breathing — no per-entity
    resolution. It asserts "someone is here" not "Alice is here".

    Args:
        csi_data: Payload from sentinel/context/{zone}/occupancy or raw CSI
        zone: Zone name
        node_id: Node ID

    Returns:
        List of SensorAssertions
    """
    assertions = []

    present = csi_data.get("present", csi_data.get("occupied", False))
    motion = csi_data.get("motion", False)
    confidence = csi_data.get("confidence", 0.0)
    variance = csi_data.get("variance", 0.0)
    calibrated = csi_data.get("calibrated", False)

    if present:
        assertions.append(SensorAssertion(
            assertion_type=AssertionType.PRESENCE,
            entity_type=EntityType.UNKNOWN_HUMAN,
            source=SensorSource.CSI,
            node_id=node_id,
            zone=zone,
            confidence=confidence,
            evidence={
                "variance": variance,
                "calibrated": calibrated,
                "motion": motion,
            },
        ))

    if motion:
        assertions.append(SensorAssertion(
            assertion_type=AssertionType.MOTION,
            entity_type=EntityType.UNKNOWN_HUMAN,
            source=SensorSource.CSI,
            node_id=node_id,
            zone=zone,
            confidence=confidence * 0.9,  # Motion is slightly less reliable than presence
            direction="moving",
            evidence={
                "variance": variance,
            },
        ))

    # Breathing → vitals assertion
    breathing_bpm = csi_data.get("breathing_bpm")
    breathing_conf = csi_data.get("breathing_confidence", 0.0)
    if breathing_bpm and breathing_conf > 0.3:
        assertions.append(SensorAssertion(
            assertion_type=AssertionType.VITALS,
            entity_type=EntityType.UNKNOWN_HUMAN,
            source=SensorSource.CSI,
            node_id=node_id,
            zone=zone,
            confidence=breathing_conf,
            breathing_rate_bpm=breathing_bpm,
            vitals_quality="strong" if breathing_conf > 0.7 else "weak",
            evidence={
                "breathing_bpm": breathing_bpm,
                "breathing_confidence": breathing_conf,
            },
        ))

    return assertions


# ── Camera Producer ──────────────────────────────────────────────────────

def camera_to_assertions(camera_data: dict, zone: str,
                          node_id: str = "") -> list[SensorAssertion]:
    """Convert camera adapter output to SensorAssertions.

    Camera provides:
      - Face recognition → identity assertions
      - Person detection → presence assertions
      - Gait analysis → (future) identity assertions

    Args:
        camera_data: Payload from camera adapter
        zone: Zone name
        node_id: Node ID

    Returns:
        List of SensorAssertions
    """
    assertions = []

    # Face detections
    for face in camera_data.get("faces", []):
        person_id = face.get("person_id")
        confidence = face.get("confidence", 0.0)
        bbox = face.get("bbox")

        spatial = SpatialRef(bbox=bbox) if bbox else None

        if person_id:
            # Known face → identity assertion
            assertions.append(SensorAssertion(
                assertion_type=AssertionType.IDENTITY,
                entity_type=EntityType.PERSON,
                source=SensorSource.CAMERA,
                node_id=node_id,
                zone=zone,
                confidence=confidence,
                person_id=person_id,
                person_name=face.get("person_name", person_id),
                spatial=spatial,
                evidence={
                    "face_id": face.get("face_id"),
                    "embedding_distance": face.get("embedding_distance"),
                    "bbox": bbox,
                },
            ))
        else:
            # Unknown face → presence assertion
            assertions.append(SensorAssertion(
                assertion_type=AssertionType.PRESENCE,
                entity_type=EntityType.UNKNOWN_HUMAN,
                source=SensorSource.CAMERA,
                node_id=node_id,
                zone=zone,
                confidence=confidence,
                spatial=spatial,
                evidence={
                    "face_id": face.get("face_id"),
                    "bbox": bbox,
                },
            ))

    # General person detection (even without face match)
    person_count = camera_data.get("persons_detected", 0)
    if person_count > 0 and not camera_data.get("faces"):
        assertions.append(SensorAssertion(
            assertion_type=AssertionType.PRESENCE,
            entity_type=EntityType.UNKNOWN_HUMAN,
            source=SensorSource.CAMERA,
            node_id=node_id,
            zone=zone,
            confidence=0.6,  # Body detection without face is lower confidence
            evidence={
                "persons_detected": person_count,
            },
        ))

    return assertions


# ── Thermal Producer ─────────────────────────────────────────────────────

def thermal_to_assertions(thermal_data: dict, zone: str,
                           node_id: str = "") -> list[SensorAssertion]:
    """Convert thermal camera output to SensorAssertions.

    Thermal provides:
      - Human-shaped hot blobs → presence assertions with spatial data
      - Temperature anomalies → anomaly assertions

    Args:
        thermal_data: Payload from thermal adapter
        zone: Zone name
        node_id: Node ID

    Returns:
        List of SensorAssertions
    """
    assertions = []

    for blob in thermal_data.get("hot_spots", []):
        temp = blob.get("temp_c", 0)
        # Human body temp range: 30-40°C at typical thermal camera distances
        is_human_temp = 28.0 <= temp <= 42.0

        spatial = SpatialRef(
            x_mm=blob.get("x_mm") or (blob.get("x", 0) * 10),
            y_mm=blob.get("y_mm") or (blob.get("y", 0) * 10),
            accuracy_m=1.0,
        )

        entity_type = EntityType.UNKNOWN_HUMAN if is_human_temp else EntityType.ENVIRONMENTAL

        assertions.append(SensorAssertion(
            assertion_type=AssertionType.PRESENCE,
            entity_type=entity_type,
            source=SensorSource.THERMAL,
            node_id=node_id,
            zone=zone,
            confidence=0.7 if is_human_temp else 0.3,
            spatial=spatial,
            evidence={
                "temp_c": temp,
                "area_px": blob.get("area_px"),
                "is_human_temp": is_human_temp,
            },
        ))

    # Also check blob count from top-level
    blob_count = thermal_data.get("human_shaped_blobs", 0)
    if blob_count > 0 and not thermal_data.get("hot_spots"):
        assertions.append(SensorAssertion(
            assertion_type=AssertionType.PRESENCE,
            entity_type=EntityType.UNKNOWN_HUMAN,
            source=SensorSource.THERMAL,
            node_id=node_id,
            zone=zone,
            confidence=0.6,
            evidence={"human_shaped_blobs": blob_count},
        ))

    return assertions


# ── Radar Producer ───────────────────────────────────────────────────────

def radar_to_assertions(radar_data: dict, zone: str,
                         node_id: str = "") -> list[SensorAssertion]:
    """Convert radar output to SensorAssertions.

    Radar (LD2450) provides:
      - Up to 3 targets with (x, y, distance, speed)
      - Motion detection
      - (MR60BHA2): heartbeat and breathing

    Args:
        radar_data: Payload from radar sensor
        zone: Zone name
        node_id: Node ID

    Returns:
        List of SensorAssertions
    """
    assertions = []

    for target in radar_data.get("targets", []):
        x_mm = target.get("x_mm", 0)
        y_mm = target.get("y_mm", 0)
        distance_mm = target.get("distance_mm", 0)
        speed_mms = target.get("speed_mms", 0)

        spatial = SpatialRef(
            x_mm=x_mm,
            y_mm=y_mm,
            distance_m=distance_mm / 1000.0 if distance_mm else None,
            accuracy_m=0.3,  # Radar is spatially precise
        )

        # Determine direction from speed
        if abs(speed_mms) < 50:
            direction = "stationary"
        elif speed_mms < 0:
            direction = "approaching"
        else:
            direction = "receding"

        # Presence assertion
        assertions.append(SensorAssertion(
            assertion_type=AssertionType.PRESENCE,
            entity_type=EntityType.UNKNOWN_HUMAN,
            source=SensorSource.RADAR,
            node_id=node_id,
            zone=zone,
            confidence=0.75,  # Radar is fairly reliable for presence
            spatial=spatial,
            evidence={
                "x_mm": x_mm,
                "y_mm": y_mm,
                "distance_mm": distance_mm,
                "speed_mms": speed_mms,
            },
        ))

        # Motion assertion (if moving)
        if direction != "stationary":
            assertions.append(SensorAssertion(
                assertion_type=AssertionType.MOTION,
                entity_type=EntityType.UNKNOWN_HUMAN,
                source=SensorSource.RADAR,
                node_id=node_id,
                zone=zone,
                confidence=0.8,
                speed_mms=speed_mms,
                direction=direction,
                spatial=spatial,
            ))

    # Vitals from MR60BHA2 (if available)
    breathing_bpm = radar_data.get("breathing_bpm")
    heart_rate = radar_data.get("heart_rate_bpm")
    if breathing_bpm or heart_rate:
        assertions.append(SensorAssertion(
            assertion_type=AssertionType.VITALS,
            entity_type=EntityType.UNKNOWN_HUMAN,
            source=SensorSource.RADAR,
            node_id=node_id,
            zone=zone,
            confidence=0.6,
            heart_rate_bpm=heart_rate,
            breathing_rate_bpm=breathing_bpm,
            vitals_quality="strong" if (heart_rate and breathing_bpm) else "weak",
            evidence={
                "heart_rate_bpm": heart_rate,
                "breathing_bpm": breathing_bpm,
                "breathing_detected": radar_data.get("breathing_detected", False),
            },
        ))

    return assertions
