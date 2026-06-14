"""
SENTINEL MQTT Topic Hierarchy
==============================
Single source of truth for all topic strings.
No service should hardcode topic paths — import from here.

Layers:
  sentinel/sensors/{zone}/{sensor_type}/raw     — raw characterized data from nodes
  sentinel/context/{zone}/state                  — interpreted zone state
  sentinel/context/{zone}/occupancy              — occupancy state machine per zone
  sentinel/context/home/narrative                — full home narrative (single source of truth)
  sentinel/identity/{person_id}/location         — per-person location tracking
  sentinel/identity/{person_id}/vitals           — per-person vitals
  sentinel/identity/unknown/{detection_id}       — unknown entity detections
  sentinel/system/{node_id}/health               — node health reports
  sentinel/system/brain/status                   — brain service status
  sentinel/system/alerts/{priority}              — prioritized alerts
"""

# ── Prefix ────────────────────────────────────────────────────────────────

PREFIX = "sentinel"


# ── Sensor Layer (raw characterized data in) ──────────────────────────────

class Sensors:
    """sentinel/sensors/{zone}/{sensor_type}/raw"""

    @staticmethod
    def raw(zone: str, sensor_type: str) -> str:
        return f"{PREFIX}/sensors/{zone}/{sensor_type}/raw"

    @staticmethod
    def raw_wildcard_zone(zone: str) -> str:
        """Subscribe to all sensor types in a zone."""
        return f"{PREFIX}/sensors/{zone}/+/raw"

    @staticmethod
    def raw_wildcard_all() -> str:
        """Subscribe to all raw sensor data across all zones."""
        return f"{PREFIX}/sensors/+/+/raw"


# ── Context Layer (interpreted state out) ─────────────────────────────────

class Context:
    """sentinel/context/{zone}/..."""

    @staticmethod
    def state(zone: str) -> str:
        """Full interpreted state for a zone."""
        return f"{PREFIX}/context/{zone}/state"

    @staticmethod
    def occupancy(zone: str) -> str:
        """Occupancy state machine output for a zone."""
        return f"{PREFIX}/context/{zone}/occupancy"

    @staticmethod
    def narrative() -> str:
        """Home-level narrative — the single source of truth."""
        return f"{PREFIX}/context/home/narrative"

    @staticmethod
    def state_wildcard() -> str:
        """Subscribe to all zone states."""
        return f"{PREFIX}/context/+/state"

    @staticmethod
    def occupancy_wildcard() -> str:
        """Subscribe to all zone occupancy."""
        return f"{PREFIX}/context/+/occupancy"


# ── Identity Layer ────────────────────────────────────────────────────────

class Identity:
    """sentinel/identity/{person_id}/..."""

    @staticmethod
    def location(person_id: str) -> str:
        return f"{PREFIX}/identity/{person_id}/location"

    @staticmethod
    def vitals(person_id: str) -> str:
        return f"{PREFIX}/identity/{person_id}/vitals"

    @staticmethod
    def unknown(detection_id: str) -> str:
        return f"{PREFIX}/identity/unknown/{detection_id}"

    @staticmethod
    def location_wildcard() -> str:
        return f"{PREFIX}/identity/+/location"

    @staticmethod
    def vitals_wildcard() -> str:
        return f"{PREFIX}/identity/+/vitals"


# ── System Layer ──────────────────────────────────────────────────────────

class System:
    """sentinel/system/..."""

    @staticmethod
    def health(node_id: str) -> str:
        return f"{PREFIX}/system/{node_id}/health"

    @staticmethod
    def brain_status() -> str:
        return f"{PREFIX}/system/brain/status"

    @staticmethod
    def alert(priority: str) -> str:
        """Priority: critical, warning, info"""
        return f"{PREFIX}/system/alerts/{priority}"

    @staticmethod
    def health_wildcard() -> str:
        return f"{PREFIX}/system/+/health"

    @staticmethod
    def alert_wildcard() -> str:
        return f"{PREFIX}/system/alerts/+"


# ── Sensor Types (canonical names) ────────────────────────────────────────

SENSOR_TYPES = [
    "radar",      # HLK-LD2450 24GHz
    "thermal",    # Infiray P2 Pro
    "lidar",      # YDLIDAR X4
    "camera",     # RPi Cam v2 + IR
    "acoustic",   # MEMS mic array
    "voc",        # BME688 chemical/VOC
    "barometric", # BMP390
    "vibration",  # ADXL345
    "emrf",       # Passive EM/RF
    "csi",        # WiFi CSI (ESP32-S3)
]

# ── Zone names (configured per installation) ──────────────────────────────
# These are defaults for Alice's home. Override via config.
DEFAULT_ZONES = [
    "office",
    "hallway",
    "entry",
    "bedroom",
    "kitchen",
    "living_room",
    "bathroom",
]

# ── Alert priorities ──────────────────────────────────────────────────────

PRIORITY_CRITICAL = "critical"  # Immediate action: intruder, fire, medical
PRIORITY_WARNING = "warning"    # Attention needed: anomaly, degradation
PRIORITY_INFO = "info"          # FYI: routine events, state changes
