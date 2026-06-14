"""
EMRF Intelligence Engine
=========================
Transforms raw WiFi/BLE scan data into actionable intelligence.

Philosophy: Desire → Curiosity → Drive → Action
  - DESIRE:    Know who is here, where they are, and spot the unexpected
  - CURIOSITY: Every device tells a story — RSSI is proximity, MAC is identity,
               patterns are behavior, absence is an event
  - DRIVE:     Track state across scans, build history, detect transitions
  - ACTION:    Publish rich presence assessments + events for brain/agent

What this replaces:
  Old: "known_count: 2, infra_count: 15, unknown_count: 3"
  New: "Alice is in the office (phone at 1.2m, -42dBm, present 47min).
        Unknown Samsung device detected at 4.8m — first seen 30s ago.
        Bob's phone departed 5 min ago (was in range for 3 hours)."

That's the difference between data and intelligence.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger("sentinel.intelligence.emrf")


# ── RSSI → Distance Model ────────────────────────────────────────────────
# Log-distance path loss: d = 10^((A - RSSI) / (10 * n))
#   A = RSSI at 1 meter reference distance
#   n = path loss exponent (higher = more attenuation)

RSSI_REF_1M = -40       # typical WiFi RSSI at 1 meter
PATH_LOSS_N = 3.0        # indoor with walls: 2.7-4.3, we use 3.0
RSSI_FLOOR = -90         # below this, signal is noise


def rssi_to_distance_m(rssi: int, ref_1m: int = RSSI_REF_1M, n: float = PATH_LOSS_N) -> float:
    """Convert RSSI (dBm) to estimated distance in meters.

    Returns math.inf for signals at or below the noise floor.
    This is a rough estimate — useful for relative ordering and zone inference,
    not for precise positioning.
    """
    if rssi is None or rssi <= RSSI_FLOOR:
        return float('inf')
    if rssi >= ref_1m:
        return 0.5  # closer than 1m reference
    exponent = (ref_1m - rssi) / (10.0 * n)
    return round(10.0 ** exponent, 1)


def distance_to_zone_proximity(distance_m: float) -> str:
    """Classify distance into human-meaningful proximity zones."""
    if distance_m <= 1.5:
        return "immediate"     # right next to the sensor / at the desk
    elif distance_m <= 4.0:
        return "near"          # same room, within reach
    elif distance_m <= 5.0:
        return "room"          # same room but far side (tightened from 8m to reduce wall bleed)
    elif distance_m <= 15.0:
        return "adjacent"      # next room / through a wall
    elif distance_m <= 30.0:
        return "far"           # other end of house
    else:
        return "edge"          # barely detectable


# ── OUI Vendor Lookup ─────────────────────────────────────────────────────
# First 3 bytes (24 bits) of MAC = OUI (Organizationally Unique Identifier)
# We embed common vendors rather than shipping a 30MB IEEE database.
# Unknown OUIs get "Unknown" — the agent can flag these for investigation.

OUI_DATABASE = {
    # Apple
    "00:17:F2": "Apple", "00:1C:B3": "Apple", "3C:E0:72": "Apple",
    "A4:83:E7": "Apple", "AC:BC:32": "Apple", "F0:99:BF": "Apple",
    "C0:95:6D": "Apple", "C8:D0:83": "Apple", "50:DE:06": "Apple",
    "28:6A:BA": "Apple", "60:F8:1D": "Apple", "78:7B:8A": "Apple",
    "A8:5C:2C": "Apple", "B0:34:95": "Apple", "DC:A9:04": "Apple",
    "F4:5C:89": "Apple", "14:98:77": "Apple", "38:C9:86": "Apple",
    "70:56:81": "Apple", "8C:85:90": "Apple", "D0:E1:40": "Apple",
    "E0:C7:67": "Apple", "0C:4D:E9": "Apple", "34:36:3B": "Apple",
    "64:A2:F9": "Apple", "7C:D1:C3": "Apple", "9C:20:7B": "Apple",
    "BC:52:B7": "Apple",
    # Samsung
    "00:21:19": "Samsung", "08:D4:2B": "Samsung", "14:49:E0": "Samsung",
    "34:23:BA": "Samsung", "50:01:D9": "Samsung", "78:BD:BC": "Samsung",
    "A0:82:1F": "Samsung", "C4:73:1E": "Samsung", "E8:50:8B": "Samsung",
    "00:24:54": "Samsung", "00:26:37": "Samsung", "6C:F3:73": "Samsung",
    "B4:3A:28": "Samsung", "D0:22:BE": "Samsung", "F4:42:8F": "Samsung",
    "84:25:DB": "Samsung", "CC:3A:61": "Samsung", "FC:A8:9A": "Samsung",
    # Google / Nest
    "F8:0F:F9": "Google", "54:60:09": "Google", "A4:77:33": "Google",
    "30:FD:38": "Google", "64:16:66": "Google/Nest",
    "CC:A7:C1": "Google/Nest", "18:B4:30": "Google/Nest",
    # Amazon
    "F0:F0:A4": "Amazon", "FC:65:DE": "Amazon", "40:B4:CD": "Amazon",
    "74:C2:46": "Amazon", "A0:02:DC": "Amazon", "68:54:FD": "Amazon",
    "EC:8A:C4": "Amazon", "94:3A:91": "Amazon",
    # Sonos
    "80:4A:F2": "Sonos", "34:7E:5C": "Sonos", "B8:E9:37": "Sonos",
    "00:0E:58": "Sonos", "5C:AA:FD": "Sonos", "78:28:CA": "Sonos",
    # Roku
    "D8:31:34": "Roku", "B0:A7:37": "Roku", "C8:3A:6B": "Roku",
    # LG
    "A4:36:C7": "LG", "00:1C:62": "LG", "00:1E:75": "LG",
    "CC:2D:8C": "LG", "64:99:68": "LG", "10:68:3F": "LG",
    # TP-Link
    "BC:07:1D": "TP-Link", "50:C7:BF": "TP-Link", "60:A4:B7": "TP-Link",
    "98:DA:C4": "TP-Link", "B0:95:75": "TP-Link",
    # router vendor
    "10:7C:61": "ASUS", "2C:FD:A1": "ASUS", "60:45:CB": "ASUS",
    "AC:9E:17": "ASUS", "04:D9:F5": "ASUS",
    # Espressif (ESP32)
    "AC:A7:04": "Espressif", "F8:17:2D": "Espressif",
    "24:6F:28": "Espressif", "30:AE:A4": "Espressif",
    "3C:61:05": "Espressif", "7C:DF:A1": "Espressif",
    "B4:E6:2D": "Espressif", "C4:4F:33": "Espressif",
    "CC:50:E3": "Espressif", "EC:FA:BC": "Espressif",
    # Raspberry Pi
    "2C:CF:67": "Raspberry Pi", "B8:27:EB": "Raspberry Pi",
    "D8:3A:DD": "Raspberry Pi", "DC:A6:32": "Raspberry Pi",
    "E4:5F:01": "Raspberry Pi",
    # Intel (NICs, NUCs)
    "94:C6:91": "Intel", "00:1B:21": "Intel", "3C:97:0E": "Intel",
    "8C:EC:4B": "Intel", "D4:5D:DF": "Intel", "A0:36:9F": "Intel",
    # Roborock
    "B0:4A:39": "Roborock",
    # Drobo
    "00:1A:62": "Drobo",
    # Nvidia (Jetson)
    "3C:6D:66": "Nvidia",
    # HP
    "64:4E:D7": "HP", "3C:D9:2B": "HP", "00:21:5A": "HP",
    # Gigabyte
    "18:C0:4D": "Gigabyte",
}


def oui_lookup(mac: str) -> str:
    """Look up device vendor from MAC OUI (first 3 octets).

    Returns vendor name or 'Unknown' if not in our database.
    """
    if not mac or len(mac) < 8:
        return "Unknown"
    oui = mac.upper()[:8]  # "AA:BB:CC"
    return OUI_DATABASE.get(oui, "Unknown")


# ── Device State Machine ─────────────────────────────────────────────────

RSSI_HISTORY_SIZE = 20         # keep last 20 readings for smoothing/trending
DEPARTURE_SCANS = 3            # absent this many consecutive scans = departed
EMA_ALPHA = 0.3                # exponential moving average weight (higher = more responsive)

# ── BLE Randomized MAC Filtering ─────────────────────────────────────────
# Modern phones rotate BLE MACs every ~15 min for privacy. Each rotation
# looks like a new device arriving and old one departing — pure noise.
# We detect random MACs two ways:
#   1. Firmware reports address type (at=1 means random)
#   2. Locally-administered bit: second-least-significant bit of first octet
#      e.g. X2:XX:XX, X6:XX:XX, XA:XX:XX, XE:XX:XX are locally administered

BLE_RANDOM_MIN_SCANS = 2       # random BLE MAC must be seen N scans before emitting events
BLE_RANDOM_DEPART_SCANS = 1    # random BLE MACs declared departed after just 1 absent scan
BLE_RANDOM_PRUNE_AGE = 300     # prune departed random BLE entries after 5 min (seconds)

# ── WiFi/BLE Cross-Confirmation Tiers ──────────────────────────────────
# Cross-referencing WiFi + BLE scan results produces tiered confidence.
# A device seen on BOTH radios with a known MAC is near-certain identity.
# A BLE-random-only device with no WiFi corroboration is noise.
#
# Source combinations → per-device confidence modifier:
#   WiFi + BLE_public + known  = 0.95 (Identified — full certainty)
#   WiFi + known               = 0.85 (Identified — WiFi-only)
#   BLE_public + known         = 0.75 (Likely — BLE public only)
#   WiFi + BLE_public + unknown= 0.70 (Confirmed unknown — real device)
#   WiFi + unknown             = 0.50 (Probable device — WiFi only)
#   BLE_public + unknown       = 0.40 (Possible device — needs data)
#   BLE_random only + unknown  = 0.10 (Noise — suppress unless persistent)

CROSS_CONF_TIERS = {
    # (has_wifi, has_ble_public, is_known) → (confidence, classification)
    (True,  True,  True):  (0.95, "identified"),
    (True,  False, True):  (0.85, "identified_wifi"),
    (False, True,  True):  (0.75, "likely"),
    (True,  True,  False): (0.70, "confirmed_unknown"),
    (True,  False, False): (0.50, "probable"),
    (False, True,  False): (0.40, "possible"),
    (False, False, False): (0.10, "noise"),  # BLE-random-only fallback
}


def is_random_ble_mac(mac: str, addr_type: int | None = None) -> bool:
    """Detect if a BLE MAC address is randomized.

    Uses two signals:
      1. addr_type from firmware (1 = random address)
      2. Locally-administered bit in first octet (fallback if firmware field missing)

    The locally-administered bit is the second-least-significant bit of the
    first byte. If set, the address was generated locally (not IEEE-assigned).
    """
    # If firmware explicitly reports address type, trust it over bit-check
    if addr_type is not None:
        return addr_type >= 1  # 0 = public (real), 1+ = random

    # Fallback (no firmware field): check locally-administered bit
    if not mac or len(mac) < 2:
        return False
    try:
        first_byte = int(mac[:2], 16)
        return bool(first_byte & 0x02)  # bit 1 = locally administered
    except ValueError:
        return False


@dataclass
class DeviceState:
    """Persistent state for a single tracked device (MAC address)."""
    mac: str = ""
    first_seen: float = 0.0          # epoch when first detected
    last_seen: float = 0.0           # epoch of most recent detection
    scan_count: int = 0              # total times seen
    consecutive_absent: int = 0      # scans in a row not seen

    # RSSI tracking
    rssi_raw: int = -100             # most recent raw RSSI
    rssi_ema: float = -100.0         # exponential moving average
    rssi_history: deque = field(default_factory=lambda: deque(maxlen=RSSI_HISTORY_SIZE))
    rssi_min: int = 0                # strongest signal ever (least negative)
    rssi_max: int = -100             # weakest signal ever (most negative)

    # Derived
    distance_m: float = float('inf')
    proximity: str = "edge"
    vendor: str = "Unknown"

    # Identity (filled by adapter)
    person_id: Optional[str] = None
    person_name: Optional[str] = None
    device_label: Optional[str] = None
    infra_category: Optional[str] = None
    infra_label: Optional[str] = None

    # State
    is_present: bool = False
    arrival_time: Optional[float] = None   # epoch of most recent arrival
    departure_time: Optional[float] = None # epoch of most recent departure
    session_duration: float = 0.0          # seconds of current/last presence session

    # Movement trend
    rssi_trend: str = "stable"  # "approaching", "stable", "receding"

    # BLE randomization flag — set when MAC is detected as randomized
    is_random_ble: bool = False

    # Cross-confirmation: which radio sources have seen this device this scan
    signal_sources: set = field(default_factory=set)   # {"wifi", "ble_public", "ble_random"}
    cross_confidence: float = 0.0                       # per-device confidence from cross-confirmation
    cross_classification: str = "unknown"               # tier label from CROSS_CONF_TIERS

    def to_dict(self) -> dict:
        """Serialize for MQTT publishing (skip deque)."""
        return {
            "mac": self.mac,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "scan_count": self.scan_count,
            "rssi": self.rssi_raw,
            "rssi_smoothed": round(self.rssi_ema, 1),
            "rssi_min": self.rssi_min,
            "rssi_max": self.rssi_max,
            "distance_m": self.distance_m,
            "proximity": self.proximity,
            "vendor": self.vendor,
            "person_id": self.person_id,
            "person_name": self.person_name,
            "device_label": self.device_label,
            "infra_category": self.infra_category,
            "infra_label": self.infra_label,
            "is_present": self.is_present,
            "arrival_time": self.arrival_time,
            "session_duration": self.session_duration,
            "rssi_trend": self.rssi_trend,
            "signal_sources": sorted(self.signal_sources),
            "cross_confidence": round(self.cross_confidence, 3),
            "cross_classification": self.cross_classification,
        }


@dataclass
class EmrfEvent:
    """An event generated by EMRF intelligence — published for brain/agent."""
    event_type: str = ""    # "arrival", "departure", "new_device", "proximity_change", "anomaly"
    timestamp: float = 0.0
    mac: str = ""
    zone: str = ""

    # Identity (if known)
    person_id: Optional[str] = None
    person_name: Optional[str] = None
    device_label: Optional[str] = None
    vendor: str = "Unknown"

    # Context
    rssi: Optional[int] = None
    distance_m: Optional[float] = None
    proximity: Optional[str] = None
    duration_sec: Optional[float] = None  # for departures: how long they were present
    description: str = ""                 # human-readable

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ── EMRF Intelligence Engine ─────────────────────────────────────────────

class EmrfIntelligence:
    """Stateful intelligence engine for EMRF (WiFi/BLE) sensor data.

    Maintains persistent device tracking across scans.
    Produces enriched presence assessments and events.
    """

    def __init__(self, mac_identity: dict, infra_identity: dict,
                 ble_name_identity: dict | None = None):
        """
        Args:
            mac_identity: MAC → {"person_id", "name", "label", "type"} from config
            infra_identity: MAC → {"category", "label", "ip"} from config
            ble_name_identity: lowercase BLE name → {"person_id", "name", "label"} from config
                               Used as fallback identity when MAC doesn't match (e.g., randomized MACs)
        """
        self._mac_identity = mac_identity
        self._infra_identity = infra_identity
        self._ble_name_identity = ble_name_identity or {}

        # Track MACs that have been identified via BLE name (so we don't re-log)
        self._ble_name_resolved: dict[str, str] = {}  # MAC → person_id

        # Persistent device state across scans — keyed by uppercase MAC
        self._devices: dict[str, DeviceState] = {}

        # Scan counter
        self._scan_count: int = 0

        log.info("EmrfIntelligence initialized with %d known person MACs, %d infra MACs, %d BLE names",
                 len(mac_identity), len(infra_identity), len(self._ble_name_identity))

    @property
    def tracked_devices(self) -> dict[str, DeviceState]:
        return self._devices

    def process_scan(self, wifi_devices: list, ble_devices: list,
                     zone: str, node_id: str) -> dict:
        """Process a complete EMRF scan and return enriched intelligence.

        This is the main entry point — called by the adapter for each scan.

        Returns dict with:
          - summary: counts and high-level stats
          - persons: per-person presence assessment
          - infrastructure: per-category infra summary
          - unknowns: enriched unknown device list
          - devices: full tracked device list
          - events: list of EmrfEvent dicts generated this scan
          - confidence: overall EMRF confidence score
          - intelligence: human-readable intelligence narrative
        """
        now = time.time()
        self._scan_count += 1
        events: list[EmrfEvent] = []

        # ── Phase 0: Build per-MAC signal source map ─────────────────
        # Track which radio(s) saw each MAC this scan — the foundation
        # of cross-confirmation. A device on BOTH radios is more real.
        scan_sources: dict[str, set] = {}   # mac → {"wifi", "ble_public", "ble_random"}

        wifi_macs = set()
        for dev in wifi_devices:
            mac = dev.get("mac", "").upper().strip()
            if mac:
                wifi_macs.add(mac)
                scan_sources.setdefault(mac, set()).add("wifi")

        for dev in ble_devices:
            mac = dev.get("mac", "").upper().strip()
            if mac:
                addr_type = dev.get("at")
                if is_random_ble_mac(mac, addr_type):
                    scan_sources.setdefault(mac, set()).add("ble_random")
                else:
                    scan_sources.setdefault(mac, set()).add("ble_public")

        # Combine all devices from this scan (merge WiFi + BLE, BLE wins on collision for name/at fields)
        all_scanned = {}  # mac → device dict
        for dev in wifi_devices:
            mac = dev.get("mac", "").upper().strip()
            if mac:
                all_scanned[mac] = dev
        for dev in ble_devices:
            mac = dev.get("mac", "").upper().strip()
            if mac:
                # BLE may have richer data (name, addr_type), merge over WiFi
                if mac in all_scanned:
                    all_scanned[mac].update(dev)
                else:
                    all_scanned[mac] = dev

        # ── Phase 1: Update tracked devices with new scan data ────────
        for mac, dev_data in all_scanned.items():
            rssi = dev_data.get("rssi")
            if isinstance(rssi, str):
                try:
                    rssi = int(rssi)
                except (ValueError, TypeError):
                    rssi = None

            state = self._devices.get(mac)

            if state is None:
                # New device — create state
                state = DeviceState(
                    mac=mac,
                    first_seen=now,
                    vendor=oui_lookup(mac),
                )

                # Detect randomized BLE MAC from pre-computed signal sources
                sources = scan_sources.get(mac, set())
                if "ble_random" in sources:
                    state.is_random_ble = True
                    log.debug("Random BLE MAC detected: %s", mac)

                # Tag with identity — try MAC first, then BLE name fallback
                identity = self._mac_identity.get(mac)
                if not identity:
                    # BLE name fallback: matches against config ble_name entries
                    # This solves MAC randomization — phones broadcast stable BLE names
                    ble_name = dev_data.get("name", "")
                    if ble_name and self._ble_name_identity:
                        identity = self._ble_name_identity.get(ble_name.lower().strip())
                        if identity:
                            # Cache this MAC→person mapping for the session
                            self._ble_name_resolved[mac] = identity["person_id"]
                            log.info("BLE name match: '%s' (MAC %s) → %s",
                                     ble_name, mac, identity["name"])
                if identity:
                    state.person_id = identity["person_id"]
                    state.person_name = identity["name"]
                    state.device_label = identity.get("label", "device")
                infra = self._infra_identity.get(mac)
                if infra:
                    state.infra_category = infra["category"]
                    state.infra_label = infra["label"]

                self._devices[mac] = state

                # Generate new_device event for truly unknown devices
                # Gate: random BLE MACs must be seen multiple scans before alerting
                if not identity and not infra and not state.is_random_ble:
                    events.append(EmrfEvent(
                        event_type="new_device",
                        timestamp=now,
                        mac=mac,
                        zone=zone,
                        vendor=state.vendor,
                        rssi=rssi,
                        distance_m=rssi_to_distance_m(rssi) if rssi else None,
                        description=f"New unknown device: {state.vendor} {mac} at {rssi} dBm",
                    ))

            # Late-binding BLE name resolution: device was seen before without a name,
            # but this scan includes a BLE name. Upgrade identity if still unidentified.
            if not state.person_id and not state.infra_category and self._ble_name_identity:
                ble_name = dev_data.get("name", "")
                if ble_name and mac not in self._ble_name_resolved:
                    identity_late = self._ble_name_identity.get(ble_name.lower().strip())
                    if identity_late:
                        state.person_id = identity_late["person_id"]
                        state.person_name = identity_late["name"]
                        state.device_label = identity_late.get("label", "device")
                        self._ble_name_resolved[mac] = identity_late["person_id"]
                        log.info("Late BLE name match: '%s' (MAC %s) → %s",
                                 ble_name, mac, identity_late["name"])

            # Was this device previously departed? → Arrival event
            was_absent = not state.is_present
            if was_absent and state.scan_count > 0:
                # Re-arrival — suppress for random BLE MACs until they prove persistent
                state.arrival_time = now
                emit_arrival = True
                if state.is_random_ble and state.scan_count < BLE_RANDOM_MIN_SCANS:
                    emit_arrival = False  # too transient, wait for more scans
                if emit_arrival:
                    ev = EmrfEvent(
                        event_type="arrival",
                        timestamp=now,
                        mac=mac,
                        zone=zone,
                        person_id=state.person_id,
                        person_name=state.person_name,
                        device_label=state.device_label,
                        vendor=state.vendor,
                        rssi=rssi,
                        description=self._describe_arrival(state, rssi),
                    )
                    events.append(ev)
            elif state.scan_count == 0:
                # First ever detection
                state.arrival_time = now

            # Update state with new reading
            state.last_seen = now
            state.scan_count += 1
            state.consecutive_absent = 0
            state.is_present = True

            if rssi is not None and rssi > RSSI_FLOOR:
                state.rssi_raw = rssi
                state.rssi_history.append(rssi)

                # EMA smoothing
                if state.rssi_ema <= RSSI_FLOOR:
                    state.rssi_ema = float(rssi)
                else:
                    state.rssi_ema = EMA_ALPHA * rssi + (1 - EMA_ALPHA) * state.rssi_ema

                # Track min/max (min = strongest, remember RSSI is negative)
                state.rssi_min = max(state.rssi_min, rssi)  # least negative = closest
                state.rssi_max = min(state.rssi_max, rssi)  # most negative = farthest

                # Distance estimate from smoothed RSSI
                state.distance_m = rssi_to_distance_m(round(state.rssi_ema))
                state.proximity = distance_to_zone_proximity(state.distance_m)

                # Trend detection (compare recent vs older readings)
                state.rssi_trend = self._compute_trend(state.rssi_history)

            # Session duration
            if state.arrival_time:
                state.session_duration = round(now - state.arrival_time, 1)

            # ── Cross-confirmation: update signal sources for this scan ──
            sources = scan_sources.get(mac, set())
            state.signal_sources = sources  # refresh each scan (not cumulative)

            has_wifi = "wifi" in sources
            has_ble_public = "ble_public" in sources
            is_known = bool(state.person_id or state.infra_category)

            # Look up tier; BLE-random-only with no wifi/ble_public → noise
            tier_key = (has_wifi, has_ble_public, is_known)
            if tier_key in CROSS_CONF_TIERS:
                state.cross_confidence, state.cross_classification = CROSS_CONF_TIERS[tier_key]
            elif "ble_random" in sources and not has_wifi and not has_ble_public:
                # BLE-random-only, not known → noise
                state.cross_confidence = 0.10
                state.cross_classification = "noise"
            else:
                # Fallback (shouldn't hit)
                state.cross_confidence = 0.30
                state.cross_classification = "unclassified"

        # ── Phase 2: Mark absent devices, detect departures ───────────
        for mac, state in self._devices.items():
            if mac not in all_scanned and state.is_present:
                state.consecutive_absent += 1
                # Random BLE MACs depart faster (less noise in event stream)
                depart_threshold = BLE_RANDOM_DEPART_SCANS if state.is_random_ble else DEPARTURE_SCANS
                if state.consecutive_absent >= depart_threshold:
                    state.is_present = False
                    state.departure_time = now
                    # Suppress departure events for transient random BLE MACs
                    if not state.is_random_ble or state.scan_count >= BLE_RANDOM_MIN_SCANS:
                        events.append(EmrfEvent(
                            event_type="departure",
                            timestamp=now,
                            mac=mac,
                            zone=zone,
                            person_id=state.person_id,
                            person_name=state.person_name,
                            device_label=state.device_label,
                            vendor=state.vendor,
                            duration_sec=state.session_duration,
                            description=self._describe_departure(state),
                        ))

        # ── Phase 2b: Prune stale random BLE MAC entries ──────────────
        # Random BLE MACs accumulate fast (new MAC every ~15 min per device).
        # Prune departed ones aggressively to prevent unbounded state growth.
        stale_randoms = [
            mac for mac, s in self._devices.items()
            if s.is_random_ble and not s.is_present
            and s.departure_time and (now - s.departure_time) > BLE_RANDOM_PRUNE_AGE
        ]
        for mac in stale_randoms:
            del self._devices[mac]
        if stale_randoms:
            log.debug("Pruned %d stale random BLE entries", len(stale_randoms))

        # ── Phase 3: Build intelligence output ────────────────────────
        present_devices = {m: s for m, s in self._devices.items() if s.is_present}

        # Per-person presence assessment
        persons = self._assess_persons(present_devices, zone, now)

        # Infrastructure summary
        infra_summary = self._assess_infrastructure(present_devices)

        # Unknown devices — the interesting ones
        unknowns = self._assess_unknowns(present_devices, zone)

        # Counts — use cross-confirmation to filter noise from unknowns
        person_device_count = sum(1 for s in present_devices.values() if s.person_id)
        infra_device_count = sum(1 for s in present_devices.values() if s.infra_category)
        # Unknown = not known person, not infra, not noise-tier, not transient random BLE
        unknown_device_count = sum(1 for s in present_devices.values()
                                   if not s.person_id and not s.infra_category
                                   and s.cross_classification != "noise"
                                   and not (s.is_random_ble and s.scan_count < BLE_RANDOM_MIN_SCANS))
        noise_count = sum(1 for s in present_devices.values()
                          if s.cross_classification == "noise")

        # Confidence — weighted by cross-confirmation tiers
        # Exclude infrastructure devices: routers, speakers, TVs etc. are
        # always present and inflate confidence even in empty rooms.
        total_present = len(present_devices)
        non_infra_present = sum(1 for s in present_devices.values()
                                if not s.infra_category)
        base_confidence = min(0.4 + (non_infra_present / 20.0) * 0.3, 0.7) if non_infra_present > 0 else 0.1

        # Person boost scaled by cross-confirmation quality
        person_boost = 0.0
        for s in present_devices.values():
            if s.person_id:
                # Full cross-confirmed person worth more than WiFi-only
                person_boost += s.cross_confidence * 0.15
        person_boost = min(person_boost, 0.3)

        proximity_boost = 0.0
        for s in present_devices.values():
            if s.person_id and s.proximity in ("immediate", "near"):
                proximity_boost = max(proximity_boost, 0.1)
        confidence = min(base_confidence + person_boost + proximity_boost, 0.95)

        # Intelligence narrative
        narrative = self._build_narrative(persons, unknowns, zone, now)

        return {
            # Backward-compatible counts
            "wifi_devices": len(wifi_devices),
            "ble_devices": len(ble_devices),
            "total_devices": total_present,
            "known_count": person_device_count,
            "infra_count": infra_device_count,
            "unknown_count": unknown_device_count,
            "noise_count": noise_count,
            "raw_scan_total": len(wifi_devices) + len(ble_devices),

            # Rich intelligence
            "persons": persons,
            "infrastructure": infra_summary,
            "unknowns": unknowns,
            "devices": {m: s.to_dict() for m, s in present_devices.items()},
            "events": [e.to_dict() for e in events],
            "scan_number": self._scan_count,
            "tracked_total": len(self._devices),
            "present_total": total_present,
            "intelligence": narrative,

            # Keep raw device lists for backward compat
            "wifi": wifi_devices,
            "ble": ble_devices,

            # Confidence
            "_confidence": round(confidence, 3),
        }

    # ── Person Presence Assessment ────────────────────────────────────

    def _assess_persons(self, present: dict, zone: str, now: float) -> dict:
        """Build per-person presence intelligence.

        Returns: {person_id: {name, zone_confidence, devices: [{...}], proximity, duration, status}}
        """
        persons: dict = {}
        for mac, state in present.items():
            if not state.person_id:
                continue
            pid = state.person_id
            if pid not in persons:
                persons[pid] = {
                    "name": state.person_name or pid,
                    "devices": [],
                    "count": 0,
                    "best_rssi": -100,
                    "closest_distance_m": float('inf'),
                    "closest_proximity": "edge",
                    "zone_confidence": 0.0,
                    "duration_sec": 0,
                    "status": "present",
                }
            p = persons[pid]
            p["count"] += 1
            p["devices"].append({
                "mac": mac,
                "label": state.device_label or "unknown",
                "rssi": state.rssi_raw,
                "rssi_smoothed": round(state.rssi_ema, 1),
                "distance_m": state.distance_m,
                "proximity": state.proximity,
                "trend": state.rssi_trend,
                "session_duration": state.session_duration,
                "cross_confidence": state.cross_confidence,
                "signal_sources": sorted(state.signal_sources),
            })

            # Track best signal = most confident position
            if state.rssi_raw > p["best_rssi"]:
                p["best_rssi"] = state.rssi_raw
                p["closest_distance_m"] = state.distance_m
                p["closest_proximity"] = state.proximity

            # Zone confidence: immediate/near = high, room = medium, far = low
            prox_conf = {
                "immediate": 0.95, "near": 0.85, "room": 0.65,
                "adjacent": 0.35, "far": 0.15, "edge": 0.05,
            }
            p["zone_confidence"] = max(p["zone_confidence"],
                                        prox_conf.get(state.proximity, 0.1))

            # Longest session across devices
            p["duration_sec"] = max(p["duration_sec"], state.session_duration)

        # Determine status based on proximity and duration
        for pid, p in persons.items():
            if p["closest_proximity"] in ("immediate", "near"):
                if p["duration_sec"] > 300:
                    p["status"] = "settled"  # been close for > 5 min
                else:
                    p["status"] = "active"
            elif p["closest_proximity"] == "room":
                p["status"] = "present"
            elif p["closest_proximity"] in ("adjacent", "far"):
                p["status"] = "distant"
            else:
                p["status"] = "edge"

            # Round for cleanliness
            p["zone_confidence"] = round(p["zone_confidence"], 2)
            p["closest_distance_m"] = round(p["closest_distance_m"], 1) if p["closest_distance_m"] != float('inf') else None
            p["duration_sec"] = round(p["duration_sec"])

        return persons

    # ── Infrastructure Assessment ─────────────────────────────────────

    def _assess_infrastructure(self, present: dict) -> dict:
        """Summarize present infrastructure by category."""
        categories: dict = {}
        for mac, state in present.items():
            cat = state.infra_category
            if not cat:
                continue
            if cat not in categories:
                categories[cat] = {"count": 0, "devices": []}
            categories[cat]["count"] += 1
            label = state.infra_label or state.vendor
            if label not in categories[cat]["devices"]:
                categories[cat]["devices"].append(label)
        return categories

    # ── Unknown Device Assessment ─────────────────────────────────────

    def _assess_unknowns(self, present: dict, zone: str) -> list:
        """Build enriched profiles of unknown devices.

        This is where curiosity kicks in — every unknown device is a question:
        Who are you? Why are you here? Are you a threat?
        """
        unknowns = []
        for mac, state in present.items():
            if state.person_id or state.infra_category:
                continue
            # Skip transient random BLE MACs — they're noise, not unknowns worth reporting
            if state.is_random_ble and state.scan_count < BLE_RANDOM_MIN_SCANS:
                continue
            # Skip noise-tier devices (BLE-random-only with no WiFi cross-confirmation)
            if state.cross_classification == "noise":
                continue
            unknowns.append({
                "mac": mac,
                "vendor": state.vendor,
                "rssi": state.rssi_raw,
                "rssi_smoothed": round(state.rssi_ema, 1),
                "distance_m": state.distance_m,
                "proximity": state.proximity,
                "first_seen": state.first_seen,
                "session_duration": state.session_duration,
                "scan_count": state.scan_count,
                "trend": state.rssi_trend,
                "threat_level": self._assess_threat(state),
                "cross_confidence": state.cross_confidence,
                "cross_classification": state.cross_classification,
                "signal_sources": sorted(state.signal_sources),
            })
        # Sort by threat level descending, then by distance ascending
        threat_order = {"high": 0, "medium": 1, "low": 2, "none": 3}
        unknowns.sort(key=lambda u: (threat_order.get(u["threat_level"], 4), u.get("distance_m", 999)))
        return unknowns

    def _assess_threat(self, state: DeviceState) -> str:
        """Assess threat level of an unknown device.

        Factors:
          - Proximity: closer = higher concern
          - Duration: lingering unknown device = suspicious
          - Vendor: known consumer brands are less suspicious
          - History: device that appears and disappears repeatedly
        """
        score = 0

        # Proximity factor
        if state.proximity == "immediate":
            score += 3
        elif state.proximity == "near":
            score += 2
        elif state.proximity == "room":
            score += 1

        # Duration factor — an unknown device that's been here a while
        if state.session_duration > 600:    # > 10 min
            score += 2
        elif state.session_duration > 120:  # > 2 min
            score += 1

        # Unknown vendor is more suspicious
        if state.vendor == "Unknown":
            score += 1

        # Known consumer brand with edge proximity = probably a neighbor
        if state.vendor in ("Apple", "Samsung", "Google") and state.proximity in ("far", "edge"):
            score = max(score - 2, 0)

        # Random BLE MAC = almost certainly a rotating phone/wearable, not an intruder
        # Dramatically reduce threat — these are privacy-rotated addresses
        if state.is_random_ble:
            score = max(score - 3, 0)

        if score >= 4:
            return "high"
        elif score >= 2:
            return "medium"
        elif score >= 1:
            return "low"
        return "none"

    # ── Trend Detection ──────────────────────────────────────────────

    @staticmethod
    def _compute_trend(rssi_history: deque) -> str:
        """Detect movement direction from RSSI history.

        Compares the mean of recent readings vs older readings.
        Rising RSSI = approaching, falling = receding.
        """
        if len(rssi_history) < 6:
            return "stable"

        readings = list(rssi_history)
        mid = len(readings) // 2
        older = sum(readings[:mid]) / mid
        newer = sum(readings[mid:]) / (len(readings) - mid)
        delta = newer - older

        if delta > 3:    # signal getting stronger = closer
            return "approaching"
        elif delta < -3:  # signal getting weaker = farther
            return "receding"
        return "stable"

    # ── Event Descriptions ────────────────────────────────────────────

    def _describe_arrival(self, state: DeviceState, rssi: Optional[int]) -> str:
        if state.person_id:
            name = state.person_name or state.person_id
            label = state.device_label or "device"
            dist = rssi_to_distance_m(rssi) if rssi else None
            dist_str = f" at ~{dist}m" if dist and dist < 100 else ""
            return f"{name}'s {label} arrived{dist_str}"
        vendor = state.vendor if state.vendor != "Unknown" else ""
        return f"{vendor} device {state.mac} arrived".strip()

    def _describe_departure(self, state: DeviceState) -> str:
        duration = self._format_duration(state.session_duration)
        if state.person_id:
            name = state.person_name or state.person_id
            label = state.device_label or "device"
            return f"{name}'s {label} departed (was present {duration})"
        return f"{state.vendor} {state.mac} departed (was present {duration})"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds / 60)}m"
        else:
            h = int(seconds / 3600)
            m = int((seconds % 3600) / 60)
            return f"{h}h {m}m"

    # ── Intelligence Narrative ────────────────────────────────────────

    def _build_narrative(self, persons: dict, unknowns: list,
                         zone: str, now: float) -> str:
        """Build human-readable intelligence summary for the brain/agent.

        This is what makes Sentinel different — not "3 devices detected"
        but "Alice is at her desk (phone at 1.2m, present 47 minutes).
        Unknown Samsung device at edge of range — likely a neighbor."
        """
        parts = []

        # People — include cross-confirmation quality
        for pid, p in persons.items():
            name = p["name"]
            dist = p["closest_distance_m"]
            dist_str = f" ~{dist}m away" if dist else ""
            dur = self._format_duration(p["duration_sec"])
            dev_count = p["count"]
            dev_word = "device" if dev_count == 1 else "devices"

            # Summarize signal quality from best device
            best_dev = max(p["devices"], key=lambda d: d.get("cross_confidence", 0)) if p["devices"] else None
            if best_dev and best_dev.get("signal_sources"):
                src_str = "+".join(s.replace("ble_", "BLE-") for s in best_dev["signal_sources"])
                conf_str = f" [{src_str}, conf={best_dev['cross_confidence']:.2f}]"
            else:
                conf_str = ""

            parts.append(
                f"{name}: {p['status']} in {zone}{dist_str}, "
                f"{dev_count} {dev_word}, present {dur}{conf_str}"
            )

        # Unknowns worth mentioning
        notable_unknowns = [u for u in unknowns if u["threat_level"] in ("high", "medium")]
        for u in notable_unknowns[:3]:  # cap at 3 to avoid noise
            vendor = u["vendor"] if u["vendor"] != "Unknown" else "unidentified"
            dist = u.get("distance_m")
            dist_str = f" ~{dist}m" if dist and dist < 100 else ""
            dur = self._format_duration(u.get("session_duration", 0))
            parts.append(
                f"ALERT: {vendor} device ({u['mac'][-8:]}){dist_str}, "
                f"threat={u['threat_level']}, present {dur}"
            )

        if not parts:
            parts.append(f"No identified persons in {zone}")

        return " | ".join(parts)
