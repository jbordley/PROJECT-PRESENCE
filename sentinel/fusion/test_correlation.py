#!/usr/bin/env python3
"""
Quick integration test for the assertion schema + correlation engine.

Simulates a real scenario:
  - EMRF detects Alice's phone (identity assertion)
  - CSI detects presence + motion (presence/motion assertions)
  - Camera detects Alice's face (identity assertion)
  → Correlation should produce a confirmed entity with high confidence.

Also tests anomaly detection:
  - CSI detects presence but EMRF sees nothing → body_without_device
  - EMRF sees device but CSI says empty → device_without_body

Run: python -m sentinel.fusion.test_correlation
"""

import sys
import os
import time

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sentinel.schemas.assertions import (
    SensorAssertion, AssertionType, SensorSource, EntityType, SpatialRef,
)
from sentinel.fusion.correlation import CorrelationEngine
from sentinel.fusion.assertion_producers import (
    emrf_to_assertions, csi_to_assertions, camera_to_assertions,
    radar_to_assertions, thermal_to_assertions,
)


def test_multi_sensor_identity():
    """Test: EMRF + Camera both identify Alice → confirmed identity."""
    print("\n═══ Test: Multi-sensor identity confirmation ═══")
    engine = CorrelationEngine()

    # Simulate EMRF intelligence output
    emrf_result = {
        "persons": {
            "alice": {
                "name": "Alice",
                "count": 1,
                "devices": [{"mac": "AA:BB:CC:DD:EE:FF", "label": "phone",
                             "rssi": -42, "cross_confidence": 0.85,
                             "signal_sources": ["wifi", "ble_public"]}],
                "closest_distance_m": 1.2,
                "closest_proximity": "immediate",
                "zone_confidence": 0.85,
                "duration_sec": 2800,
                "status": "settled",
            }
        },
        "unknowns": [],
    }

    # Simulate camera output
    camera_result = {
        "faces": [{
            "person_id": "alice",
            "person_name": "Alice",
            "confidence": 0.92,
            "face_id": "face_001",
            "bbox": {"x": 100, "y": 50, "w": 200, "h": 200},
        }],
    }

    # Convert to assertions
    emrf_assertions = emrf_to_assertions(emrf_result, zone="office", node_id="node_1")
    camera_assertions = camera_to_assertions(camera_result, zone="office", node_id="<broker-host>")

    print(f"  EMRF produced {len(emrf_assertions)} assertions")
    for a in emrf_assertions:
        print(f"    → {a.assertion_type.value}: {a.person_id or a.device_mac}, conf={a.confidence:.2f}")

    print(f"  Camera produced {len(camera_assertions)} assertions")
    for a in camera_assertions:
        print(f"    → {a.assertion_type.value}: {a.person_id or 'unknown'}, conf={a.confidence:.2f}")

    # Ingest and correlate
    engine.ingest_batch(emrf_assertions)
    engine.ingest_batch(camera_assertions)
    result = engine.correlate("office")

    print(f"\n  Correlation result:")
    print(f"    Entities: {result['stats']['entity_count']}")
    print(f"    Known persons: {result['stats']['known_count']}")
    for entity in result["entities"]:
        print(f"    → {entity.get('entity_id')}: "
              f"identity_conf={entity.get('identity_confidence', 0):.2f}, "
              f"status={entity.get('status')}, "
              f"sources={entity.get('supporting_sources')}")

    # Verify
    known = result["known_persons"]
    assert len(known) >= 1, f"Expected at least 1 known person, got {len(known)}"
    alice = known[0]
    assert alice.get("identity_confidence", 0) >= 0.90, \
        f"Expected identity_confidence >= 0.90, got {alice.get('identity_confidence')}"
    assert alice.get("status") == "confirmed", \
        f"Expected status=confirmed, got {alice.get('status')}"
    print("  ✅ PASS: Alice confirmed with high confidence")


def test_body_without_device():
    """Test: CSI presence + no EMRF → body_without_device anomaly."""
    print("\n═══ Test: Body without device detection ═══")
    engine = CorrelationEngine()

    # CSI says someone is here
    csi_data = {
        "present": True,
        "motion": True,
        "variance": 45.0,
        "calibrated": True,
        "confidence": 0.7,
    }
    csi_assertions = csi_to_assertions(csi_data, zone="office", node_id="node_1")

    print(f"  CSI produced {len(csi_assertions)} assertions (no EMRF assertions)")

    engine.ingest_batch(csi_assertions)
    result = engine.correlate("office")

    print(f"  Anomalies: {len(result['anomalies'])}")
    for a in result["anomalies"]:
        print(f"    → {a.get('type')}: {a.get('description', '')[:80]}")

    has_body_without_device = any(
        a.get("type") == "body_without_device" for a in result["anomalies"]
    )
    assert has_body_without_device, "Expected body_without_device anomaly"
    print("  ✅ PASS: Body without device anomaly detected")


def test_device_without_body():
    """Test: EMRF identity + no CSI presence → device_without_body anomaly."""
    print("\n═══ Test: Device left behind detection ═══")
    engine = CorrelationEngine()

    # EMRF sees Alice's phone but CSI says nobody home
    emrf_result = {
        "persons": {
            "alice": {
                "name": "Alice",
                "count": 1,
                "devices": [{"mac": "AA:BB:CC:DD:EE:FF", "label": "phone",
                             "rssi": -55, "cross_confidence": 0.85,
                             "signal_sources": ["wifi"]}],
                "closest_distance_m": 3.0,
                "closest_proximity": "near",
                "zone_confidence": 0.7,
                "duration_sec": 7200,
                "status": "present",
            }
        },
        "unknowns": [],
    }

    emrf_assertions = emrf_to_assertions(emrf_result, zone="office", node_id="node_1")
    # No CSI assertions (CSI says empty)

    print(f"  EMRF produced {len(emrf_assertions)} assertions (no CSI assertions)")

    engine.ingest_batch(emrf_assertions)
    result = engine.correlate("office")

    print(f"  Anomalies: {len(result['anomalies'])}")
    for a in result["anomalies"]:
        print(f"    → {a.get('type')}: {a.get('description', '')[:80]}")

    has_device_left = any(
        a.get("type") == "device_without_body" for a in result["anomalies"]
    )
    assert has_device_left, "Expected device_without_body anomaly"
    print("  ✅ PASS: Device left behind anomaly detected")


def test_assertion_producers():
    """Test that all producers generate valid assertions."""
    print("\n═══ Test: All assertion producers ═══")

    # EMRF
    emrf_result = {
        "persons": {"alice": {"name": "Alice", "count": 1,
                     "devices": [{"mac": "AA:BB:CC:DD:EE:FF", "rssi": -42,
                                  "cross_confidence": 0.85, "signal_sources": ["wifi"]}],
                     "closest_distance_m": 1.0, "closest_proximity": "immediate",
                     "zone_confidence": 0.9, "duration_sec": 300, "status": "settled"}},
        "unknowns": [{"mac": "11:22:33:44:55:66", "vendor": "Samsung", "rssi": -65,
                       "distance_m": 5.0, "proximity": "room", "cross_confidence": 0.5,
                       "session_duration": 60, "cross_classification": "probable",
                       "signal_sources": ["wifi"], "threat_level": "low", "scan_count": 5}],
    }
    emrf_a = emrf_to_assertions(emrf_result, "office", "node_1")
    assert len(emrf_a) >= 2, f"EMRF: expected >= 2 assertions, got {len(emrf_a)}"
    print(f"  EMRF: {len(emrf_a)} assertions ✅")

    # CSI
    csi_data = {"present": True, "motion": True, "variance": 45, "calibrated": True,
                "confidence": 0.7, "breathing_bpm": 16, "breathing_confidence": 0.8}
    csi_a = csi_to_assertions(csi_data, "office", "node_1")
    assert len(csi_a) >= 2, f"CSI: expected >= 2 assertions, got {len(csi_a)}"
    print(f"  CSI: {len(csi_a)} assertions ✅")

    # Camera
    cam_data = {"faces": [{"person_id": "alice", "person_name": "Alice",
                            "confidence": 0.9, "bbox": {"x": 100, "y": 50, "w": 200, "h": 200}}]}
    cam_a = camera_to_assertions(cam_data, "office", "<broker-host>")
    assert len(cam_a) >= 1, f"Camera: expected >= 1 assertions, got {len(cam_a)}"
    print(f"  Camera: {len(cam_a)} assertions ✅")

    # Thermal
    therm_data = {"hot_spots": [{"temp_c": 35.2, "x": 120, "y": 80, "area_px": 400}]}
    therm_a = thermal_to_assertions(therm_data, "office", "node_1")
    assert len(therm_a) >= 1, f"Thermal: expected >= 1 assertions, got {len(therm_a)}"
    print(f"  Thermal: {len(therm_a)} assertions ✅")

    # Radar
    radar_data = {"targets": [{"x_mm": 1200, "y_mm": 800, "distance_mm": 1440, "speed_mms": -200}],
                  "breathing_bpm": 16, "heart_rate_bpm": 72}
    radar_a = radar_to_assertions(radar_data, "office", "node_1")
    assert len(radar_a) >= 2, f"Radar: expected >= 2 assertions, got {len(radar_a)}"
    print(f"  Radar: {len(radar_a)} assertions ✅")

    print("  ✅ All producers generate valid assertions")


def test_full_scenario():
    """Full scenario: all sensors reporting for one zone."""
    print("\n═══ Test: Full multi-sensor scenario ═══")
    engine = CorrelationEngine()

    # All sensors report
    emrf_a = emrf_to_assertions({
        "persons": {"alice": {"name": "Alice", "count": 1,
                     "devices": [{"mac": "AA:BB:CC:DD:EE:FF", "rssi": -42,
                                  "cross_confidence": 0.85, "signal_sources": ["wifi", "ble_public"],
                                  "label": "phone"}],
                     "closest_distance_m": 1.2, "closest_proximity": "immediate",
                     "zone_confidence": 0.85, "duration_sec": 2800, "status": "settled"}},
        "unknowns": [],
    }, "office", "node_1")

    csi_a = csi_to_assertions({
        "present": True, "motion": False, "variance": 12.0,
        "calibrated": True, "confidence": 0.75,
        "breathing_bpm": 15, "breathing_confidence": 0.8,
    }, "office", "node_1")

    cam_a = camera_to_assertions({
        "faces": [{"person_id": "alice", "person_name": "Alice",
                    "confidence": 0.92, "bbox": {"x": 100, "y": 50, "w": 200, "h": 200}}],
    }, "office", "<broker-host>")

    radar_a = radar_to_assertions({
        "targets": [{"x_mm": 1000, "y_mm": 500, "distance_mm": 1118, "speed_mms": 0}],
    }, "office", "node_1")

    all_assertions = emrf_a + csi_a + cam_a + radar_a
    engine.ingest_batch(all_assertions)
    result = engine.correlate("office")

    print(f"  Total assertions: {len(all_assertions)}")
    print(f"  Entities: {result['stats']['entity_count']}")
    print(f"  Known: {result['stats']['known_count']}")
    print(f"  Unknown: {result['stats']['unknown_count']}")
    print(f"  Anomalies: {len(result['anomalies'])}")

    for entity in result["entities"]:
        print(f"  Entity: {entity.get('entity_id')}")
        print(f"    identity_conf: {entity.get('identity_confidence', 0):.2f}")
        print(f"    presence_conf: {entity.get('presence_confidence', 0):.2f}")
        print(f"    status: {entity.get('status')}")
        print(f"    sources: {entity.get('supporting_sources')}")
        if entity.get('breathing_rate_bpm'):
            print(f"    breathing: {entity.get('breathing_rate_bpm')} bpm")

    known = result["known_persons"]
    assert len(known) >= 1, "Expected at least 1 known person"
    print("  ✅ PASS: Full multi-sensor correlation successful")


if __name__ == "__main__":
    print("SENTINEL Correlation Engine — Integration Tests")
    print("=" * 55)

    test_assertion_producers()
    test_multi_sensor_identity()
    test_body_without_device()
    test_device_without_body()
    test_full_scenario()

    print("\n" + "=" * 55)
    print("All tests passed! ✅")
