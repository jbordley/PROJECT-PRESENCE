"""
SENTINEL Assertion Schema
===========================
The common language between sensors and the fusion/correlation layer.

Philosophy:
  Sensors don't know the truth — they make CLAIMS about what they observe.
  Each claim is an "assertion" with a type, confidence, and evidence chain.
  The correlation layer matches assertions across sensors to build truth.

  CSI says "someone is here" (confidence 0.7).
  EMRF says "Alice's phone is at 1.2m" (confidence 0.85).
  Camera says "face=alice" (confidence 0.92).
  → Correlation: Alice is here. Confidence: 0.95. Three independent confirmations.

  CSI says "someone is here" (confidence 0.7).
  EMRF says nothing.
  Camera says "unknown face".
  → Correlation: Unknown person present WITHOUT a phone. Threat escalation.

Assertion Types:
  - presence:   "A human/device is detected in this zone"
  - identity:   "This detection belongs to person X"
  - motion:     "Movement is occurring"
  - position:   "Detection is at spatial coordinates (x, y, distance)"
  - vitals:     "Biological signals detected (heart rate, breathing)"
  - absence:    "A previously-present entity is no longer detected"
  - anomaly:    "Something unexpected is happening"

Each assertion carries:
  - source:     Which sensor made this claim
  - confidence: How sure the sensor is (0.0-1.0)
  - evidence:   Raw data backing the claim (for audit/debugging)
  - spatial:    Where in the zone (if applicable)
  - temporal:   When, and for how long

The correlation layer consumes assertions from ALL sensors for a zone
and produces correlated entities — the real picture of who/what is where.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Any


# ── Assertion Types ──────────────────────────────────────────────────────

class AssertionType(str, Enum):
    """What the sensor is claiming."""
    PRESENCE = "presence"       # Something/someone is here
    IDENTITY = "identity"       # This detection is person X
    MOTION = "motion"           # Movement detected
    POSITION = "position"       # Spatial coordinates of detection
    VITALS = "vitals"           # Biological signals (HR, breathing)
    ABSENCE = "absence"         # Previously-present entity is gone
    ANOMALY = "anomaly"         # Something unexpected


class SensorSource(str, Enum):
    """Which sensor produced this assertion."""
    EMRF = "emrf"               # WiFi/BLE radio frequency
    CSI = "csi"                 # WiFi Channel State Information
    CAMERA = "camera"           # Visual (IR day/night + face recognition)
    THERMAL = "thermal"         # Thermal camera (TC001 Y16)
    RADAR = "radar"             # mmWave radar (LD2450 / MR60BHA2)
    ACOUSTIC = "acoustic"       # Microphone array (future)
    BAROMETRIC = "barometric"   # Pressure sensor (door events)
    VIBRATION = "vibration"     # Vibration sensor (footsteps)


class EntityType(str, Enum):
    """What kind of thing was detected."""
    PERSON = "person"           # Known identified person
    DEVICE = "device"           # Known device (phone, laptop)
    INFRASTRUCTURE = "infrastructure"  # Known infra (router, thermostat)
    UNKNOWN_HUMAN = "unknown_human"    # Human-shaped but unidentified
    UNKNOWN_DEVICE = "unknown_device"  # Device but not in config
    ANIMAL = "animal"           # Pet detection (future, camera-based)
    ENVIRONMENTAL = "environmental"    # Non-entity event (door, HVAC)


# ── Spatial Reference ────────────────────────────────────────────────────

@dataclass
class SpatialRef:
    """Where in the zone the detection is located.

    Not all sensors provide all fields. EMRF gives distance only (from RSSI).
    Radar gives (x, y, distance). Camera gives bounding box. Thermal gives
    pixel coordinates that map to room coordinates via calibration.

    The correlation layer uses whatever spatial data is available to match
    assertions across sensors — even partial overlap is useful.
    """
    distance_m: Optional[float] = None      # Distance from sensor
    x_mm: Optional[int] = None              # Cartesian X (radar/thermal)
    y_mm: Optional[int] = None              # Cartesian Y (radar/thermal)
    z_mm: Optional[int] = None              # Height (future, ToF)
    bearing_deg: Optional[float] = None     # Angle from sensor (future)
    bbox: Optional[dict] = None             # Camera bounding box {x,y,w,h}
    proximity: Optional[str] = None         # Qualitative: immediate/near/room/far/edge
    accuracy_m: Optional[float] = None      # Estimated accuracy of position

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ── Core Assertion ───────────────────────────────────────────────────────

@dataclass
class SensorAssertion:
    """A single claim made by a sensor about what it observes.

    This is the atomic unit of the fusion system. Every sensor produces
    zero or more assertions per scan cycle. The correlation layer consumes
    all assertions for a zone and builds the unified picture.

    Design principles:
      1. Assertions are IMMUTABLE once created — sensors don't edit them
      2. Every assertion has a confidence — nothing is certain
      3. Evidence is preserved for audit/debugging
      4. Spatial data is optional but enables correlation
      5. Assertions expire — stale data is worse than no data
    """
    # ── What ──
    assertion_type: AssertionType = AssertionType.PRESENCE
    entity_type: EntityType = EntityType.UNKNOWN_HUMAN

    # ── Who claims it ──
    source: SensorSource = SensorSource.CSI
    node_id: str = ""
    zone: str = ""

    # ── How sure ──
    confidence: float = 0.0     # 0.0-1.0, sensor's self-assessed confidence

    # ── Identity (for IDENTITY assertions) ──
    person_id: Optional[str] = None
    person_name: Optional[str] = None
    device_mac: Optional[str] = None
    device_label: Optional[str] = None

    # ── Spatial ──
    spatial: Optional[SpatialRef] = None

    # ── Temporal ──
    timestamp: float = field(default_factory=time.time)
    first_seen: Optional[float] = None      # When this entity was first detected
    duration_sec: float = 0.0               # How long it's been detected
    ttl_sec: float = 30.0                   # Assertion expires after this many seconds

    # ── Motion (for MOTION assertions) ──
    speed_mms: Optional[int] = None         # mm/s (from radar)
    direction: Optional[str] = None         # "approaching", "receding", "lateral", "stationary"

    # ── Vitals (for VITALS assertions) ──
    heart_rate_bpm: Optional[float] = None
    breathing_rate_bpm: Optional[float] = None
    vitals_quality: Optional[str] = None    # "strong", "weak", "intermittent"

    # ── Evidence (raw data backing this claim) ──
    evidence: dict = field(default_factory=dict)
    # Examples:
    #   EMRF: {"mac": "AA:BB:CC:DD:EE:FF", "rssi": -42, "signal_sources": ["wifi", "ble_public"]}
    #   CSI:  {"variance": 45.2, "calibrated": true}
    #   Camera: {"face_embedding_dist": 0.23, "bbox": [100, 50, 200, 200]}
    #   Thermal: {"temp_c": 34.2, "blob_area_px": 450}
    #   Radar: {"target_index": 0, "raw_x": 1200, "raw_y": 800}

    # ── Anomaly detail (for ANOMALY assertions) ──
    anomaly_type: Optional[str] = None      # "device_without_body", "body_without_device", etc.
    anomaly_description: Optional[str] = None

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.timestamp) > self.ttl_sec

    @property
    def age_sec(self) -> float:
        return time.time() - self.timestamp

    def to_dict(self) -> dict:
        d = {}
        for k, v in asdict(self).items():
            if v is None:
                continue
            if isinstance(v, Enum):
                d[k] = v.value
            elif isinstance(v, SpatialRef):
                d[k] = v.to_dict()
            elif isinstance(v, dict) and not v:
                continue  # skip empty dicts
            else:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> SensorAssertion:
        """Deserialize from dict (e.g., from MQTT JSON)."""
        # Convert string enums back
        if "assertion_type" in d:
            d["assertion_type"] = AssertionType(d["assertion_type"])
        if "entity_type" in d:
            d["entity_type"] = EntityType(d["entity_type"])
        if "source" in d:
            d["source"] = SensorSource(d["source"])
        if "spatial" in d and isinstance(d["spatial"], dict):
            d["spatial"] = SpatialRef(**d["spatial"])
        # Filter to known fields
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# ── Correlated Entity ────────────────────────────────────────────────────
# This is what the correlation layer PRODUCES after matching assertions.

@dataclass
class CorrelatedEntity:
    """A unified entity built by correlating assertions across sensors.

    This represents what the system BELIEVES to be true after considering
    all available evidence. It's the bridge between raw sensor assertions
    and the brain's narrative.

    Example:
      Three assertions arrive:
        1. EMRF: identity(alice, phone, confidence=0.85, distance=1.2m)
        2. CSI: presence(confidence=0.70, zone=office)
        3. Camera: identity(alice, face, confidence=0.92)
      → CorrelatedEntity:
          entity_id: "alice"
          entity_type: PERSON
          identity_confidence: 0.96 (multi-sensor agreement)
          position_confidence: 0.85 (EMRF distance is best spatial data)
          supporting_assertions: [emrf_assertion, csi_assertion, camera_assertion]
          status: "confirmed"
    """
    # ── Identity ──
    entity_id: str = ""                     # person_id, or generated UUID for unknowns
    entity_type: EntityType = EntityType.UNKNOWN_HUMAN
    person_id: Optional[str] = None
    person_name: Optional[str] = None

    # ── Location ──
    zone: str = ""
    best_position: Optional[SpatialRef] = None  # Best spatial estimate from all sources
    position_confidence: float = 0.0

    # ── State ──
    identity_confidence: float = 0.0        # How sure we are of WHO this is
    presence_confidence: float = 0.0        # How sure we are they're HERE
    motion_state: str = "unknown"           # "stationary", "moving", "approaching", "departing"
    activity_hint: str = ""                 # Inferred from motion pattern + position

    # ── Vitals ──
    heart_rate_bpm: Optional[float] = None
    breathing_rate_bpm: Optional[float] = None
    vitals_confidence: float = 0.0

    # ── Evidence chain ──
    supporting_sources: list = field(default_factory=list)  # ["emrf", "csi", "camera"]
    assertion_count: int = 0                # How many assertions contributed
    first_seen: float = 0.0
    duration_sec: float = 0.0
    last_updated: float = field(default_factory=time.time)

    # ── Status ──
    status: str = "tentative"              # "tentative", "confirmed", "stale"
    # tentative = single-sensor only
    # confirmed = multi-sensor agreement
    # stale = no recent assertions

    # ── Threat assessment (for unknowns) ──
    threat_level: str = "none"             # "none", "low", "medium", "high"
    threat_reasons: list = field(default_factory=list)

    # ── Anomalies detected during correlation ──
    anomalies: list = field(default_factory=list)
    # Examples:
    #   "device_without_body": EMRF sees phone, CSI/thermal/radar see nobody
    #   "body_without_device": CSI+thermal see person, EMRF sees no devices
    #   "identity_conflict":   Camera says person A, EMRF says person B's device

    def to_dict(self) -> dict:
        d = {}
        for k, v in asdict(self).items():
            if v is None:
                continue
            if isinstance(v, Enum):
                d[k] = v.value
            elif isinstance(v, SpatialRef):
                d[k] = v.to_dict()
            elif isinstance(v, (list, dict)) and not v:
                continue
            else:
                d[k] = v
        return d


# ── Correlation Rules (declarative) ──────────────────────────────────────
# These define how assertions from different sensors relate to each other.
# The correlation engine evaluates these rules each cycle.

@dataclass
class CorrelationRule:
    """A declarative rule for matching assertions across sensors.

    Rules define WHEN to correlate and WHAT conclusion to draw.
    The correlation engine evaluates all rules each cycle.

    Example rules:
      "If EMRF says identity=X AND Camera says identity=X → boost confidence to 0.95"
      "If CSI says presence AND EMRF says no devices → flag body_without_device"
      "If EMRF says device present AND CSI says absent → flag device_left_behind"
    """
    rule_id: str = ""
    name: str = ""
    description: str = ""

    # Required assertion types to trigger this rule
    requires: list = field(default_factory=list)
    # Each entry: {"source": "emrf", "type": "identity"} or {"source": "csi", "type": "presence"}

    # Negative conditions (assertions that must NOT be present)
    excludes: list = field(default_factory=list)

    # What to do when the rule fires
    action: str = ""            # "boost_confidence", "flag_anomaly", "merge_entities", "escalate_threat"
    action_params: dict = field(default_factory=dict)

    # Rule priority (higher = evaluated first)
    priority: int = 0


# ── Pre-defined Correlation Rules ────────────────────────────────────────

CORRELATION_RULES = [
    # ── Identity Confirmation ──
    CorrelationRule(
        rule_id="multi_sensor_identity",
        name="Multi-sensor identity confirmation",
        description="EMRF device + Camera face agree on same person → near-certain identity",
        requires=[
            {"source": "emrf", "type": "identity"},
            {"source": "camera", "type": "identity"},
        ],
        action="boost_confidence",
        action_params={"identity_confidence": 0.96, "status": "confirmed"},
        priority=100,
    ),
    CorrelationRule(
        rule_id="emrf_csi_presence",
        name="EMRF + CSI presence agreement",
        description="WiFi device detected + CSI presence = person with their device",
        requires=[
            {"source": "emrf", "type": "presence"},
            {"source": "csi", "type": "presence"},
        ],
        action="boost_confidence",
        action_params={"presence_confidence_boost": 0.15},
        priority=90,
    ),
    CorrelationRule(
        rule_id="triple_presence",
        name="Triple-sensor presence",
        description="EMRF + CSI + thermal/radar all agree → very high confidence",
        requires=[
            {"source": "emrf", "type": "presence"},
            {"source": "csi", "type": "presence"},
            {"source": "thermal", "type": "presence"},  # or radar
        ],
        action="boost_confidence",
        action_params={"presence_confidence": 0.95, "status": "confirmed"},
        priority=95,
    ),

    # ── Anomaly Detection ──
    CorrelationRule(
        rule_id="body_without_device",
        name="Body without device",
        description="CSI/thermal/radar detect human presence but EMRF sees no unknown devices → "
                    "person without phone (could be intruder, could be household member who left phone)",
        requires=[
            {"source": "csi", "type": "presence"},
        ],
        excludes=[
            {"source": "emrf", "type": "presence"},
        ],
        action="flag_anomaly",
        action_params={
            "anomaly_type": "body_without_device",
            "threat_boost": 1,
            "description": "Human presence detected but no device found — possible intruder or phoneless person",
        },
        priority=85,
    ),
    CorrelationRule(
        rule_id="device_without_body",
        name="Device left behind",
        description="EMRF sees a known person's device but CSI/thermal say nobody is present → "
                    "device was left behind, person has departed",
        requires=[
            {"source": "emrf", "type": "identity"},
        ],
        excludes=[
            {"source": "csi", "type": "presence"},
        ],
        action="flag_anomaly",
        action_params={
            "anomaly_type": "device_without_body",
            "description": "Device detected but no human presence — device may be left behind",
        },
        priority=80,
    ),
    CorrelationRule(
        rule_id="identity_conflict",
        name="Identity conflict",
        description="Camera face recognition and EMRF device identity disagree on who is present",
        requires=[
            {"source": "emrf", "type": "identity"},
            {"source": "camera", "type": "identity"},
        ],
        action="flag_anomaly",
        action_params={
            "anomaly_type": "identity_conflict",
            "description": "Camera and EMRF disagree on identity — investigate",
        },
        priority=70,
    ),

    # ── Intruder Detection ──
    CorrelationRule(
        rule_id="unknown_close_presence",
        name="Unknown person in close proximity",
        description="Unknown human detected at close range by multiple sensors → potential intruder",
        requires=[
            {"source": "emrf", "type": "presence"},  # unknown device
            {"source": "csi", "type": "presence"},
        ],
        action="escalate_threat",
        action_params={
            "threat_level": "medium",
            "description": "Unknown person with unknown device detected by multiple sensors",
        },
        priority=75,
    ),

    # ── Activity Inference ──
    CorrelationRule(
        rule_id="stationary_detection",
        name="Stationary person detection",
        description="CSI presence + radar stationary + known identity → person is settled",
        requires=[
            {"source": "csi", "type": "presence"},
            {"source": "radar", "type": "motion"},  # speed ~= 0
        ],
        action="infer_activity",
        action_params={"activity_hint": "stationary", "motion_state": "stationary"},
        priority=50,
    ),
]
