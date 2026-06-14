"""
SENTINEL Message Schemas
=========================
Every MQTT message in the system is defined here as a dataclass.
All timestamps are UTC epoch seconds with millisecond precision.
All messages serialize to JSON via .to_json() and deserialize via .from_dict().

Layer 1 — Sensor:   Raw characterized data from nodes (with confidence certificates)
Layer 2 — Context:  Interpreted zone state (fusion output)
Layer 3 — Identity: Per-person tracking
Layer 4 — System:   Health, status, alerts
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


def _now() -> float:
    return round(time.time(), 3)


# ── Enums ─────────────────────────────────────────────────────────────────

class SensorHealth(str, Enum):
    """Layer 1 — Sensor Health Gate output."""
    NOMINAL = "nominal"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class OccupancyState(str, Enum):
    """Per-person, per-zone occupancy state machine.
    ABSENT → APPROACHING → PRESENT → ACTIVE → SETTLED → SLEEPING
                                       ↕
                                 TRANSITIONING
    """
    ABSENT = "absent"
    APPROACHING = "approaching"
    PRESENT = "present"
    ACTIVE = "active"
    SETTLED = "settled"
    SLEEPING = "sleeping"
    TRANSITIONING = "transitioning"


class HomeState(str, Enum):
    """Home-level occupancy summary."""
    EMPTY = "empty"
    OCCUPIED_SINGLE = "occupied_single"
    OCCUPIED_MULTIPLE = "occupied_multiple"
    UNKNOWN = "unknown"


class AlertPriority(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# ── Base ──────────────────────────────────────────────────────────────────

@dataclass
class SentinelMessage:
    """Base for all Sentinel messages."""
    timestamp: float = field(default_factory=_now)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Convert enums to their string values
        for k, v in d.items():
            if isinstance(v, Enum):
                d[k] = v.value
            elif isinstance(v, list):
                d[k] = [
                    item.value if isinstance(item, Enum)
                    else (item.to_dict() if hasattr(item, 'to_dict') else item)
                    for item in v
                ]
            elif isinstance(v, dict):
                d[k] = {
                    dk: dv.value if isinstance(dv, Enum) else dv
                    for dk, dv in v.items()
                }
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_json(cls, s: str):
        return cls.from_dict(json.loads(s))


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 1 — SENSOR MESSAGES (raw characterized data from nodes)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class EnvironmentReading(SentinelMessage):
    """Environmental baseline sampled by node each cycle (Step 1 of node processing)."""
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    pressure_hpa: Optional[float] = None
    em_baseline_dbm: Optional[float] = None


@dataclass
class SensorReading(SentinelMessage):
    """
    A single characterized sensor reading — the confidence certificate.
    This is what the node sends after its full processing cycle (Steps 1-7).

    Published to: sentinel/sensors/{zone}/{sensor_type}/raw
    """
    node_id: str = ""
    zone: str = ""
    sensor_type: str = ""           # from topics.SENSOR_TYPES

    # The reading itself (sensor-specific payload)
    reading: dict = field(default_factory=dict)

    # Confidence certificate
    confidence: float = 0.0         # 0.0-1.0, computed by node
    health: str = SensorHealth.NOMINAL.value

    # Environmental context at time of reading
    environment: dict = field(default_factory=dict)

    # Plausibility flags (Layer 2 checks done on-node)
    physics_plausible: bool = True
    physics_notes: str = ""         # why it was flagged, if flagged


# ── Sensor-specific reading payloads (go inside SensorReading.reading) ────

# These are documented here as reference for what each sensor puts in
# the 'reading' dict. Not enforced as dataclasses to keep the node
# firmware simple (it builds dicts directly).

RADAR_READING_SCHEMA = {
    "targets": [                    # up to 3 targets for LD2450
        {
            "x_mm": 0,             # position relative to sensor
            "y_mm": 0,
            "speed_mms": 0,        # mm/s, negative = approaching
            "distance_mm": 0,
        }
    ],
    "target_count": 0,
    "breathing_detected": False,
    "breathing_bpm": None,
}

CSI_READING_SCHEMA = {
    "present": False,
    "motion": False,
    "variance": 0.0,
    "amplitude_mean": 0.0,
    "calibrated": False,
    "breathing_bpm": None,
    "breathing_confidence": 0.0,
}

THERMAL_READING_SCHEMA = {
    "max_temp_c": 0.0,
    "min_temp_c": 0.0,
    "mean_temp_c": 0.0,
    "hot_spots": [],                # [{x, y, temp_c, area_px}]
    "human_shaped_blobs": 0,
}

CAMERA_READING_SCHEMA = {
    "faces_detected": 0,
    "faces": [],                    # [{face_id, confidence, bbox}]
    "persons_detected": 0,
    "gait_signature": None,
}

BAROMETRIC_READING_SCHEMA = {
    "pressure_hpa": 0.0,
    "delta_hpa": 0.0,              # change from baseline
    "event": None,                  # "door_open", "window_open", None
}

VIBRATION_READING_SCHEMA = {
    "magnitude_g": 0.0,
    "dominant_freq_hz": 0.0,
    "event": None,                  # "footstep", "door_impact", "glass_break", None
}


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 2 — CONTEXT MESSAGES (fusion output → brain input)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ZoneOccupancy(SentinelMessage):
    """
    Occupancy state for one zone, after sensor fusion.
    Published to: sentinel/context/{zone}/occupancy
    """
    zone: str = ""
    occupied: bool = False
    occupant_count: int = 0
    occupants: list = field(default_factory=list)  # list of person_ids or "unknown_N"

    # Per-occupant state
    states: dict = field(default_factory=dict)  # {person_id: OccupancyState}

    # Fusion confidence
    confidence: float = 0.0
    contributing_sensors: list = field(default_factory=list)  # which sensors agree
    dissenting_sensors: list = field(default_factory=list)    # which disagree (informative)


@dataclass
class ZoneState(SentinelMessage):
    """
    Full interpreted state for a zone.
    Published to: sentinel/context/{zone}/state
    """
    zone: str = ""
    occupancy: dict = field(default_factory=dict)  # ZoneOccupancy as dict

    # Environmental summary
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    air_quality: Optional[str] = None  # "normal", "elevated_voc", "anomalous"

    # Activity summary
    activity_level: str = "none"   # "none", "low", "moderate", "high"
    dominant_activity: str = ""     # "working", "cooking", "sleeping", etc.

    # Recent events
    events: list = field(default_factory=list)  # last N events in this zone


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 3 — NARRATIVE (brain output — the single source of truth)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ActorState:
    """Per-person state within the narrative."""
    person_id: str = ""
    display_name: str = ""             # Config "name" field, e.g. "Alice"
    identity_confidence: float = 0.0   # 0.0-1.0
    current_zone: str = ""
    previous_zone: str = ""
    transition_time: Optional[float] = None
    occupancy_state: str = OccupancyState.ABSENT.value
    activity: str = ""                  # "working", "cooking", "sleeping"

    # Vitals (when available)
    heart_rate_bpm: Optional[float] = None
    breathing_bpm: Optional[float] = None
    vitals_confidence: float = 0.0

    # Narrative description
    description: str = ""  # "Alice is at her desk, settled, working"


@dataclass
class Anomaly:
    """A current anomalous condition."""
    anomaly_id: str = ""
    type: str = ""                     # "unknown_person", "vital_deviation", "sensor_failure"
    zone: str = ""
    description: str = ""
    severity: str = AlertPriority.INFO.value
    first_detected: float = 0.0
    last_updated: float = 0.0
    resolved: bool = False


@dataclass
class NarrativeState(SentinelMessage):
    """
    The complete home narrative — SINGLE SOURCE OF TRUTH.
    Published to: sentinel/context/home/narrative

    Every service that needs to understand what's happening subscribes here.
    No service queries sensors directly.
    """
    # Home-level summary
    home_state: str = HomeState.UNKNOWN.value
    total_occupants: int = 0
    known_occupants: int = 0
    unknown_occupants: int = 0

    # Per-actor state
    actors: list = field(default_factory=list)  # list of ActorState dicts

    # Per-zone summary
    zone_states: dict = field(default_factory=dict)  # {zone: ZoneState as dict}

    # Anomalies
    anomalies: list = field(default_factory=list)  # list of Anomaly dicts

    # Human-readable narrative
    summary: str = ""  # "Alice is in the office, settled. Home is quiet. No anomalies."

    # Brain metadata
    narrative_version: int = 0         # increments on every update
    brain_uptime_sec: float = 0.0
    last_event: str = ""               # what triggered this update


# ═══════════════════════════════════════════════════════════════════════════
# LAYER 4 — SYSTEM MESSAGES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class NodeHealth(SentinelMessage):
    """
    Node self-reported health.
    Published to: sentinel/system/{node_id}/health
    """
    node_id: str = ""
    zone: str = ""
    uptime_sec: float = 0.0

    # Per-sensor health
    sensor_health: dict = field(default_factory=dict)  # {sensor_type: SensorHealth}

    # Environment
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    free_heap_bytes: Optional[int] = None
    wifi_rssi: Optional[int] = None


@dataclass
class BrainStatus(SentinelMessage):
    """
    Brain service status heartbeat.
    Published to: sentinel/system/brain/status
    """
    status: str = "online"             # "online", "degraded", "offline"
    tier: int = 3                      # 1=node, 2=zone brain, 3=primary brain
    uptime_sec: float = 0.0
    narrative_version: int = 0
    zones_tracked: list = field(default_factory=list)
    nodes_online: int = 0
    nodes_degraded: int = 0


@dataclass
class Alert(SentinelMessage):
    """
    Prioritized alert.
    Published to: sentinel/system/alerts/{priority}
    """
    priority: str = AlertPriority.INFO.value
    source: str = ""                   # "brain", "node_office_1", "fusion"
    zone: str = ""
    title: str = ""
    description: str = ""
    actor: str = ""                    # person_id if relevant
    requires_action: bool = False
