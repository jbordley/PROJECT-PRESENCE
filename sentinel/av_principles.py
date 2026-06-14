#!/usr/bin/env python3
"""
SENTINEL AV Principles — Autonomous Vehicle Patterns as Code
==============================================================
Six design patterns transferred from autonomous vehicle development,
implemented as the operational framework for the Meta-Reasoner.

These are NOT separate concerns from the Meta-Reasoner — they ARE
the Meta-Reasoner's operating framework. Each pattern maps to one
or more of the four Meta-Reasoner components:

    Curiosity → surfaces what is unresolved
    Desire    → defines what resolution looks like
    Drive     → sustains the system until resolution
    Action    → commits resolved intent as system behavior

AV Patterns → Meta-Reasoner Mapping:
    1. Dynamic Sensor Confidence Weighting  → Drive Engine
    2. Shadow Mode                          → Curiosity Engine
    3. Fast Path / Slow Path Split          → Desire Engine
    4. Explicit Degradation Modes           → Desire Engine
    5. Long Tail Logging                    → Curiosity Engine
    6. Structured Calibration Scenarios     → Drive Engine

One AV pattern explicitly DOES NOT transfer:
    Millisecond latency optimization — Sentinel's threat model is slower.
    Do not sacrifice accuracy for latency.

Integration:
    sentinel_fusion.py imports and uses these patterns in its processing
    pipeline. The Meta-Reasoner service orchestrates them through the
    four-component cycle.

Usage:
    from sentinel.av_principles import (
        TrustDecayCurve,
        ShadowRunner,
        FastSlowSplit,
        DegradationStateMachine,
        LongTailLogger,
        CalibrationScenarioRunner,
        MetaReasonerOrchestrator,
    )
"""

from __future__ import annotations

import json
import logging
import time
import threading
import hashlib
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Any

log = logging.getLogger("sentinel.av_principles")


# ═════════════════════════════════════════════════════════════════════════════
# AV PATTERN 1 — Dynamic Sensor Confidence Weighting (Drive Engine)
# ═════════════════════════════════════════════════════════════════════════════
#
# Every sensor earns its trust through consistent performance. Trust decays
# when readings become stale, erratic, or contradicted by other sensors.
# This is the Drive Engine: the persistence mechanism that keeps the system
# calibrated between immediate rewards (good readings) and the long-term
# goal (reliable presence detection).
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class SensorTrustState:
    """Runtime trust state for one sensor in one zone."""
    sensor_type: str = ""
    zone: str = ""
    base_weight: float = 0.7        # Configured trust ceiling
    current_weight: float = 0.7     # Live trust after decay/boost
    last_reading_time: float = 0.0  # Epoch when last valid reading arrived
    consecutive_valid: int = 0      # Run of plausible readings
    consecutive_invalid: int = 0    # Run of rejected readings
    total_readings: int = 0
    agreements: int = 0             # Cross-sensor agreement count
    disagreements: int = 0          # Cross-sensor disagreement count


class TrustDecayCurve:
    """
    AV Pattern 1: Dynamic sensor confidence weighting with trust decay.

    Meta-Reasoner link: DRIVE ENGINE
    - Keeps the system funded between immediate rewards
    - Discipline in service of something larger (accurate fusion)

    Trust mechanics:
      - Freshness decay: trust drops as time-since-last-reading increases
      - Consistency boost: long runs of valid readings increase trust
      - Contradiction penalty: disagreements with other sensors reduce trust
      - Recovery: trust rebuilds slowly after penalties
      - Floor: trust never drops below a minimum (sensor still contributes)

    AV origin: Sensor confidence weighting in perception fusion. A camera
    in fog is not equally trustworthy as a camera in clear conditions.
    The AV doesn't discard it — it turns the volume down.
    """

    # Default trust weights per sensor type (from spec)
    DEFAULT_WEIGHTS = {
        "radar": 0.9,
        "thermal": 0.85,
        "camera": 0.8,
        "lidar": 0.75,
        "csi": 0.7,
        "acoustic": 0.4,
        "vibration": 0.35,
        "barometric": 0.3,
    }

    # Decay parameters
    STALE_THRESHOLD_SEC = 10.0    # Start decaying after this
    DECAY_HALF_LIFE_SEC = 30.0    # Trust halves every 30s of staleness
    MIN_TRUST = 0.05              # Floor: never fully ignore a sensor
    CONSISTENCY_BOOST = 0.005     # Per valid reading trust boost
    CONTRADICTION_PENALTY = 0.05  # Per disagreement trust reduction
    RECOVERY_RATE = 0.002         # Slow trust recovery per valid reading after penalty

    def __init__(self):
        self._sensors: dict[tuple[str, str], SensorTrustState] = {}
        self._lock = threading.Lock()

    def get_or_create(self, zone: str, sensor_type: str) -> SensorTrustState:
        """Get or initialize trust state for a sensor/zone pair."""
        key = (zone, sensor_type)
        with self._lock:
            if key not in self._sensors:
                base = self.DEFAULT_WEIGHTS.get(sensor_type, 0.5)
                self._sensors[key] = SensorTrustState(
                    sensor_type=sensor_type,
                    zone=zone,
                    base_weight=base,
                    current_weight=base,
                )
            return self._sensors[key]

    def record_reading(self, zone: str, sensor_type: str, valid: bool,
                       agreed_with_others: Optional[bool] = None):
        """
        Record a sensor reading outcome and update trust.

        Args:
            zone: Zone name
            sensor_type: Sensor type string
            valid: Whether the reading passed plausibility checks
            agreed_with_others: If cross-sensor check was performed, did it agree?
        """
        state = self.get_or_create(zone, sensor_type)
        now = time.time()

        with self._lock:
            state.last_reading_time = now
            state.total_readings += 1

            if valid:
                state.consecutive_valid += 1
                state.consecutive_invalid = 0
                # Consistency boost (capped at base weight)
                boost = min(
                    self.CONSISTENCY_BOOST,
                    state.base_weight - state.current_weight
                )
                if boost > 0:
                    state.current_weight += boost
            else:
                state.consecutive_invalid += 1
                state.consecutive_valid = 0
                # Invalidity penalty
                state.current_weight = max(
                    state.current_weight - self.CONTRADICTION_PENALTY,
                    self.MIN_TRUST,
                )

            # Cross-sensor agreement/disagreement
            if agreed_with_others is True:
                state.agreements += 1
                state.current_weight = min(
                    state.current_weight + self.RECOVERY_RATE,
                    state.base_weight,
                )
            elif agreed_with_others is False:
                state.disagreements += 1
                state.current_weight = max(
                    state.current_weight - self.CONTRADICTION_PENALTY,
                    self.MIN_TRUST,
                )

    def get_weight(self, zone: str, sensor_type: str) -> float:
        """
        Get the current effective trust weight for a sensor, including
        freshness decay. Call this at fusion time, not at recording time.
        """
        state = self.get_or_create(zone, sensor_type)
        now = time.time()

        with self._lock:
            if state.last_reading_time == 0:
                return state.current_weight

            staleness = now - state.last_reading_time
            if staleness <= self.STALE_THRESHOLD_SEC:
                return state.current_weight

            # Exponential decay based on staleness beyond threshold
            excess = staleness - self.STALE_THRESHOLD_SEC
            decay_factor = 0.5 ** (excess / self.DECAY_HALF_LIFE_SEC)
            decayed = state.current_weight * decay_factor
            return max(decayed, self.MIN_TRUST)

    def get_all_weights(self, zone: str) -> dict[str, float]:
        """Get current trust weights for all sensors in a zone."""
        result = {}
        with self._lock:
            for (z, st), state in self._sensors.items():
                if z == zone:
                    result[st] = self.get_weight(zone, st)
        return result

    def to_dict(self) -> dict:
        """Serialize all trust states for diagnostics/publishing."""
        with self._lock:
            return {
                f"{s.zone}/{s.sensor_type}": {
                    "base_weight": s.base_weight,
                    "current_weight": round(s.current_weight, 4),
                    "effective_weight": round(self.get_weight(s.zone, s.sensor_type), 4),
                    "total_readings": s.total_readings,
                    "consecutive_valid": s.consecutive_valid,
                    "agreements": s.agreements,
                    "disagreements": s.disagreements,
                }
                for s in self._sensors.values()
            }


# ═════════════════════════════════════════════════════════════════════════════
# AV PATTERN 2 — Shadow Mode (Curiosity Engine)
# ═════════════════════════════════════════════════════════════════════════════
#
# New models or algorithm changes run in parallel with production, receiving
# the same inputs but not affecting outputs. The system compares shadow
# results against production results to build confidence before promotion.
#
# This is the Curiosity Engine: it pulls toward the unknown by testing
# new hypotheses without risking system integrity.
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ShadowResult:
    """Result from a shadow model run."""
    shadow_id: str = ""
    description: str = ""
    input_hash: str = ""          # Hash of the input data for correlation
    shadow_output: Any = None     # What the shadow model predicted
    production_output: Any = None # What production actually decided
    agreed: bool = False          # Did shadow agree with production?
    timestamp: float = 0.0


class ShadowRunner:
    """
    AV Pattern 2: Shadow mode — silent parallel model runs with promotion gate.

    Meta-Reasoner link: CURIOSITY ENGINE
    - Pulls toward the unknown
    - Tests new hypotheses safely
    - Directional but not targeted

    Shadow models receive the same sensor inputs as production but their
    outputs are logged, not acted upon. When a shadow model demonstrates
    sufficient agreement with production (or demonstrates better accuracy
    against ground truth), it can be promoted to production.

    AV origin: New perception models must shadow-run for millions of miles
    before they replace production. No exceptions.
    """

    # Promotion requires this many shadow runs
    MIN_SHADOW_RUNS = 100
    # And this agreement rate with production
    MIN_AGREEMENT_RATE = 0.95
    # Maximum shadow history to retain
    MAX_HISTORY = 1000

    def __init__(self):
        self._shadows: dict[str, Callable] = {}      # id → shadow function
        self._descriptions: dict[str, str] = {}       # id → human description
        self._history: dict[str, deque] = {}          # id → deque of ShadowResult
        self._promoted: set[str] = set()              # ids that have been promoted
        self._lock = threading.Lock()

    def register_shadow(self, shadow_id: str, fn: Callable,
                        description: str = ""):
        """
        Register a shadow model. The callable receives the same arguments
        as the production function and returns a comparable output.
        """
        with self._lock:
            self._shadows[shadow_id] = fn
            self._descriptions[shadow_id] = description
            self._history[shadow_id] = deque(maxlen=self.MAX_HISTORY)
            log.info("Shadow registered: %s — %s", shadow_id, description)

    def run_shadow(self, shadow_id: str, production_output: Any,
                   *args, **kwargs) -> Optional[ShadowResult]:
        """
        Run a shadow model with the same inputs as production.
        Compares output and logs the result. Never affects system behavior.

        Args:
            shadow_id: Which shadow to run
            production_output: What production decided (for comparison)
            *args, **kwargs: Same inputs given to production
        """
        with self._lock:
            fn = self._shadows.get(shadow_id)
            if fn is None:
                return None

        try:
            shadow_out = fn(*args, **kwargs)
        except Exception as e:
            log.debug("Shadow %s raised: %s", shadow_id, e)
            return None

        # Hash inputs for correlation
        input_repr = str(args) + str(sorted(kwargs.items()))
        input_hash = hashlib.md5(input_repr.encode()).hexdigest()[:12]

        agreed = (shadow_out == production_output)

        result = ShadowResult(
            shadow_id=shadow_id,
            description=self._descriptions.get(shadow_id, ""),
            input_hash=input_hash,
            shadow_output=shadow_out,
            production_output=production_output,
            agreed=agreed,
            timestamp=time.time(),
        )

        with self._lock:
            self._history[shadow_id].append(result)

        return result

    def check_promotion_ready(self, shadow_id: str) -> tuple[bool, dict]:
        """
        Check if a shadow model is ready for promotion to production.

        Returns:
            (ready, stats) — ready is True if promotion criteria are met.
            stats includes run count, agreement rate, and recommendation.
        """
        with self._lock:
            history = list(self._history.get(shadow_id, []))

        total = len(history)
        if total < self.MIN_SHADOW_RUNS:
            return False, {
                "shadow_id": shadow_id,
                "total_runs": total,
                "required_runs": self.MIN_SHADOW_RUNS,
                "status": "insufficient_data",
            }

        agreements = sum(1 for r in history if r.agreed)
        rate = agreements / total

        ready = rate >= self.MIN_AGREEMENT_RATE
        return ready, {
            "shadow_id": shadow_id,
            "total_runs": total,
            "agreement_rate": round(rate, 4),
            "required_rate": self.MIN_AGREEMENT_RATE,
            "ready": ready,
            "status": "promotion_ready" if ready else "needs_improvement",
        }

    def promote(self, shadow_id: str) -> bool:
        """Mark a shadow as promoted. Actual swap is the caller's responsibility."""
        with self._lock:
            if shadow_id in self._shadows:
                self._promoted.add(shadow_id)
                log.info("Shadow PROMOTED: %s", shadow_id)
                return True
            return False

    def get_stats(self) -> dict:
        """Diagnostic stats for all shadow models."""
        with self._lock:
            return {
                sid: {
                    "description": self._descriptions.get(sid, ""),
                    "total_runs": len(hist),
                    "agreement_rate": round(
                        sum(1 for r in hist if r.agreed) / max(len(hist), 1), 4
                    ),
                    "promoted": sid in self._promoted,
                }
                for sid, hist in self._history.items()
            }


# ═════════════════════════════════════════════════════════════════════════════
# AV PATTERN 3 — Fast Path / Slow Path Split (Desire Engine)
# ═════════════════════════════════════════════════════════════════════════════
#
# Critical alerts take a fast path that cannot be blocked by the agent's
# slow reasoning. If someone is detected and the system is in alarm mode,
# the alert fires immediately — it doesn't wait for the narrative engine
# to finish constructing a poetic world model.
#
# This is the Desire Engine: it pulls toward a known goal state (safety)
# with direction and motivation toward a defined outcome.
# ═════════════════════════════════════════════════════════════════════════════

class AlertPriority(str, Enum):
    """Alert priority levels for fast path routing."""
    CRITICAL = "critical"   # Fast path: immediate, cannot block
    WARNING = "warning"     # Fast path: within 1 second
    INFO = "info"           # Slow path: batched with narrative


@dataclass
class FastPathAlert:
    """An alert that bypasses the slow reasoning pipeline."""
    alert_id: str = ""
    priority: AlertPriority = AlertPriority.INFO
    zone: str = ""
    description: str = ""
    sensor_sources: list = field(default_factory=list)
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)
    routed_fast: bool = False  # True if this went through the fast path


class FastSlowSplit:
    """
    AV Pattern 3: Independent fast path for critical alerts.

    Meta-Reasoner link: DESIRE ENGINE
    - Pulls toward a known goal state (home security, occupant safety)
    - Direction and motivation toward defined outcomes

    Architecture:
      - Fast path: Evaluates sensor readings directly against threat rules.
        Fires alerts immediately. Cannot be blocked by agent reasoning.
      - Slow path: Feeds sensor readings to the fusion → brain → narrative
        pipeline for rich context and reasoning.

    Both paths receive the same data. Fast path is a filter, not a fork.

    AV origin: Emergency braking doesn't wait for the route planner to
    finish re-calculating. The brake actuator has its own direct path
    from obstacle detection.
    """

    def __init__(self):
        self._fast_rules: list[Callable] = []  # Each rule: (reading) → Optional[FastPathAlert]
        self._alert_handlers: list[Callable] = []  # Subscribers for fast alerts
        self._alert_history: deque = deque(maxlen=500)
        self._lock = threading.Lock()

    def register_fast_rule(self, rule: Callable):
        """
        Register a fast-path rule. The rule receives a sensor reading dict
        and returns a FastPathAlert if the condition is met, None otherwise.

        Rules must be FAST — no network calls, no database queries.
        They are pure functions over sensor data.
        """
        with self._lock:
            self._fast_rules.append(rule)
        log.info("Fast path rule registered: %s", getattr(rule, '__name__', str(rule)))

    def register_alert_handler(self, handler: Callable):
        """
        Register a handler for fast-path alerts. Handlers are called
        immediately when an alert fires. Typical handlers: Telegram notifier,
        alarm trigger, logging service.
        """
        with self._lock:
            self._alert_handlers.append(handler)

    def evaluate(self, reading: dict) -> list[FastPathAlert]:
        """
        Evaluate a sensor reading against all fast-path rules.
        Returns list of alerts that fired. Dispatches to handlers immediately.

        This is called on EVERY sensor reading, before the slow path.
        It must be fast.
        """
        alerts = []

        with self._lock:
            rules = list(self._fast_rules)
            handlers = list(self._alert_handlers)

        for rule in rules:
            try:
                alert = rule(reading)
                if alert is not None:
                    alert.routed_fast = True
                    alert.timestamp = time.time()
                    alerts.append(alert)
            except Exception as e:
                log.error("Fast path rule error: %s", e)

        # Dispatch to handlers (non-blocking)
        for alert in alerts:
            self._alert_history.append(alert)
            for handler in handlers:
                try:
                    handler(alert)
                except Exception as e:
                    log.error("Alert handler error: %s", e)

        return alerts

    def get_recent_alerts(self, n: int = 20) -> list[FastPathAlert]:
        """Get the N most recent fast-path alerts."""
        with self._lock:
            return list(self._alert_history)[-n:]


# ═════════════════════════════════════════════════════════════════════════════
# AV PATTERN 4 — Explicit Degradation Modes (Desire Engine)
# ═════════════════════════════════════════════════════════════════════════════
#
# The system operates in one of three explicit modes based on available
# sensor coverage and confidence. Each mode has defined capabilities and
# limitations. The system knows what it can and cannot do in each mode.
#
# This is the Desire Engine: it defines what resolution looks like at
# each capability level, not just at peak performance.
# ═════════════════════════════════════════════════════════════════════════════

class SystemMode(str, Enum):
    """System operating modes based on sensor availability and confidence."""
    FULL = "full"             # All sensors nominal, high confidence
    DEGRADED = "degraded"     # Some sensors missing or unreliable
    MINIMAL = "minimal"       # Critical sensors only, safety-mode


@dataclass
class ModeCapabilities:
    """What the system can do in a given mode."""
    mode: SystemMode = SystemMode.FULL
    can_detect_presence: bool = True
    can_detect_motion: bool = True
    can_identify_person: bool = True
    can_estimate_breathing: bool = True
    can_detect_intruder: bool = True
    can_track_multi_zone: bool = True
    confidence_ceiling: float = 1.0
    active_sensors: list = field(default_factory=list)
    degraded_sensors: list = field(default_factory=list)
    offline_sensors: list = field(default_factory=list)
    notes: str = ""


class DegradationStateMachine:
    """
    AV Pattern 4: Explicit degradation modes with defined capabilities.

    Meta-Reasoner link: DESIRE ENGINE
    - Defines what resolution looks like at every capability level
    - The system always knows what it can and cannot do

    Mode transitions:
      FULL → DEGRADED: When any sensor drops below trust threshold or goes offline
      DEGRADED → MINIMAL: When primary sensor (CSI/radar) is offline
      DEGRADED → FULL: When all sensors recover to nominal
      MINIMAL → DEGRADED: When primary sensor comes back online

    Each mode has an explicit capability set. The system communicates its
    limitations honestly — it never claims confidence it doesn't have.

    AV origin: An AV in rain doesn't pretend it has the same perception as
    in sunshine. It reduces speed (confidence ceiling), increases following
    distance (temporal hold), and communicates to the driver.
    """

    # Thresholds for mode transitions
    TRUST_THRESHOLD_DEGRADED = 0.4     # Below this → sensor is degraded
    PRIMARY_SENSORS = {"csi", "radar"}  # If all of these are down → MINIMAL
    MIN_SENSORS_FOR_FULL = 2           # Need at least this many nominal sensors

    # Capability definitions per mode
    MODE_CAPABILITIES = {
        SystemMode.FULL: ModeCapabilities(
            mode=SystemMode.FULL,
            can_detect_presence=True,
            can_detect_motion=True,
            can_identify_person=True,
            can_estimate_breathing=True,
            can_detect_intruder=True,
            can_track_multi_zone=True,
            confidence_ceiling=1.0,
        ),
        SystemMode.DEGRADED: ModeCapabilities(
            mode=SystemMode.DEGRADED,
            can_detect_presence=True,
            can_detect_motion=True,
            can_identify_person=False,   # Needs camera + CSI both working
            can_estimate_breathing=False, # Needs clean CSI
            can_detect_intruder=True,
            can_track_multi_zone=True,
            confidence_ceiling=0.7,
        ),
        SystemMode.MINIMAL: ModeCapabilities(
            mode=SystemMode.MINIMAL,
            can_detect_presence=True,     # Acoustic/barometric fallback
            can_detect_motion=False,
            can_identify_person=False,
            can_estimate_breathing=False,
            can_detect_intruder=True,     # Conservative: any presence = potential intruder
            can_track_multi_zone=False,
            confidence_ceiling=0.4,
            notes="Safety mode: reduced capabilities, conservative alerting",
        ),
    }

    def __init__(self, trust_curve: TrustDecayCurve):
        self._trust = trust_curve
        self._current_mode = SystemMode.FULL
        self._mode_entered_time = time.time()
        self._transition_history: deque = deque(maxlen=100)
        self._lock = threading.Lock()

    @property
    def current_mode(self) -> SystemMode:
        with self._lock:
            return self._current_mode

    @property
    def capabilities(self) -> ModeCapabilities:
        """Get current mode's capability set."""
        return self.MODE_CAPABILITIES[self.current_mode]

    def evaluate(self, zone: str) -> SystemMode:
        """
        Evaluate current sensor state and determine operating mode.
        Call this periodically or on sensor state changes.

        Returns the (potentially new) system mode.
        """
        weights = self._trust.get_all_weights(zone)

        # Classify sensors
        nominal = []
        degraded = []
        offline = []

        for sensor_type, weight in weights.items():
            if weight < self.TRUST_THRESHOLD_DEGRADED:
                if weight <= TrustDecayCurve.MIN_TRUST:
                    offline.append(sensor_type)
                else:
                    degraded.append(sensor_type)
            else:
                nominal.append(sensor_type)

        # Determine mode
        primary_available = any(s in nominal for s in self.PRIMARY_SENSORS)
        primary_degraded = any(s in degraded for s in self.PRIMARY_SENSORS)

        if not primary_available and not primary_degraded:
            new_mode = SystemMode.MINIMAL
        elif len(nominal) < self.MIN_SENSORS_FOR_FULL or degraded:
            new_mode = SystemMode.DEGRADED
        else:
            new_mode = SystemMode.FULL

        with self._lock:
            if new_mode != self._current_mode:
                transition = {
                    "from": self._current_mode.value,
                    "to": new_mode.value,
                    "zone": zone,
                    "nominal": nominal,
                    "degraded": degraded,
                    "offline": offline,
                    "timestamp": time.time(),
                }
                self._transition_history.append(transition)
                log.info("Mode transition: %s → %s (zone=%s, nominal=%s, degraded=%s)",
                         self._current_mode.value, new_mode.value,
                         zone, nominal, degraded)
                self._current_mode = new_mode
                self._mode_entered_time = time.time()

            # Update capabilities with current sensor state
            caps = self.MODE_CAPABILITIES[new_mode]
            caps.active_sensors = nominal
            caps.degraded_sensors = degraded
            caps.offline_sensors = offline

        return new_mode

    def get_status(self) -> dict:
        """Current mode status for MQTT publishing."""
        with self._lock:
            caps = self.capabilities
            return {
                "mode": self._current_mode.value,
                "mode_since": round(self._mode_entered_time, 3),
                "confidence_ceiling": caps.confidence_ceiling,
                "can_detect_presence": caps.can_detect_presence,
                "can_identify_person": caps.can_identify_person,
                "can_estimate_breathing": caps.can_estimate_breathing,
                "active_sensors": caps.active_sensors,
                "degraded_sensors": caps.degraded_sensors,
                "offline_sensors": caps.offline_sensors,
                "recent_transitions": len(self._transition_history),
            }


# ═════════════════════════════════════════════════════════════════════════════
# AV PATTERN 5 — Long Tail Logging (Curiosity Engine)
# ═════════════════════════════════════════════════════════════════════════════
#
# Novel inputs that don't match any known pattern are flagged, stored, and
# made available for review. The system doesn't just classify — it also
# catalogs what it CANNOT classify, creating a growing inventory of the
# unknown.
#
# This is the Curiosity Engine: it treats not-knowing as the attractive
# force. Every novel input is a question the system wants answered.
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class NovelInput:
    """A sensor input that doesn't match any known pattern."""
    novel_id: str = ""
    zone: str = ""
    sensor_type: str = ""
    description: str = ""
    raw_signature: dict = field(default_factory=dict)   # The actual data
    nearest_known: str = ""        # Closest known pattern
    distance_from_known: float = 0.0  # How different from nearest known
    reviewed: bool = False         # Has a human looked at this?
    resolved_as: str = ""          # What it turned out to be
    timestamp: float = 0.0


class LongTailLogger:
    """
    AV Pattern 5: Long tail logging — novel input cataloging.

    Meta-Reasoner link: CURIOSITY ENGINE
    - Pulls toward the unknown
    - Flags unresolved uncertainty
    - Treats not-knowing as the attractive force

    When the system encounters a sensor reading that doesn't fit any known
    pattern (neither presence nor empty, or a pattern never seen before),
    it doesn't just assign "unknown" and move on. It logs the full context,
    including what it expected, what it got, and how far the actual reading
    was from any known baseline.

    Over time, this builds a catalog of edge cases that can be used for:
      - Expanding the system's pattern vocabulary
      - Identifying systematic blind spots
      - Training better models (supervised from human review)
      - Discovering new presence signatures (pets, HVAC, etc.)

    AV origin: The "long tail" of autonomous driving — rare events that
    individually are unlikely but collectively are certain. Every novel
    scenario is logged with full sensor context for offline analysis.
    """

    MAX_NOVEL_INPUTS = 10000  # Ring buffer size
    NOVELTY_THRESHOLD = 2.0  # Standard deviations from any known pattern

    def __init__(self):
        self._novel_inputs: deque = deque(maxlen=self.MAX_NOVEL_INPUTS)
        self._known_signatures: dict[str, dict] = {}  # label → signature stats
        self._unreviewed_count = 0
        self._lock = threading.Lock()

    def register_known_pattern(self, label: str, signature: dict):
        """
        Register a known sensor pattern for novelty comparison.
        Signature should include mean, std of key metrics.
        """
        with self._lock:
            self._known_signatures[label] = signature
            log.info("Known pattern registered: %s", label)

    def evaluate(self, zone: str, sensor_type: str,
                 reading: dict) -> Optional[NovelInput]:
        """
        Check if a reading is novel (doesn't match any known pattern).
        Returns a NovelInput if it is, None if it matches a known pattern.
        """
        if not self._known_signatures:
            return None  # Can't evaluate novelty without known patterns

        # Find nearest known pattern
        min_distance = float("inf")
        nearest = ""

        for label, sig in self._known_signatures.items():
            dist = self._compute_distance(reading, sig)
            if dist < min_distance:
                min_distance = dist
                nearest = label

        if min_distance <= self.NOVELTY_THRESHOLD:
            return None  # Matches a known pattern

        # Novel input detected
        novel_id = f"novel_{int(time.time()*1000)}_{zone}_{sensor_type}"
        novel = NovelInput(
            novel_id=novel_id,
            zone=zone,
            sensor_type=sensor_type,
            description=f"Reading {min_distance:.2f}σ from nearest known pattern '{nearest}'",
            raw_signature=reading,
            nearest_known=nearest,
            distance_from_known=round(min_distance, 3),
            timestamp=time.time(),
        )

        with self._lock:
            self._novel_inputs.append(novel)
            self._unreviewed_count += 1

        log.info("NOVEL INPUT: %s in %s — %.2fσ from '%s'",
                 sensor_type, zone, min_distance, nearest)
        return novel

    def _compute_distance(self, reading: dict, signature: dict) -> float:
        """
        Compute normalized distance between a reading and a known signature.
        Uses z-score distance on shared numeric keys.
        """
        distances = []
        sig_mean = signature.get("mean", {})
        sig_std = signature.get("std", {})

        for key in sig_mean:
            if key in reading and key in sig_std:
                try:
                    val = float(reading[key])
                    mean = float(sig_mean[key])
                    std = float(sig_std[key])
                    if std > 0:
                        distances.append(abs(val - mean) / std)
                except (ValueError, TypeError):
                    continue

        if not distances:
            return float("inf")

        return sum(distances) / len(distances)

    def get_unreviewed(self, limit: int = 50) -> list[NovelInput]:
        """Get unreviewed novel inputs for human inspection."""
        with self._lock:
            return [n for n in self._novel_inputs if not n.reviewed][:limit]

    def resolve(self, novel_id: str, resolved_as: str):
        """Mark a novel input as reviewed and resolved."""
        with self._lock:
            for n in self._novel_inputs:
                if n.novel_id == novel_id:
                    n.reviewed = True
                    n.resolved_as = resolved_as
                    self._unreviewed_count = max(0, self._unreviewed_count - 1)
                    log.info("Novel %s resolved as: %s", novel_id, resolved_as)
                    return True
        return False

    def get_stats(self) -> dict:
        with self._lock:
            total = len(self._novel_inputs)
            reviewed = sum(1 for n in self._novel_inputs if n.reviewed)
            return {
                "total_novel_inputs": total,
                "unreviewed": self._unreviewed_count,
                "reviewed": reviewed,
                "known_patterns": len(self._known_signatures),
            }


# ═════════════════════════════════════════════════════════════════════════════
# AV PATTERN 6 — Structured Calibration Scenarios (Drive Engine)
# ═════════════════════════════════════════════════════════════════════════════
#
# Calibration is not just "empty room for 60 seconds." It's a structured
# test track: empty room baseline, single-person walk-through, multi-person,
# known positions, known activities. Each scenario validates a specific
# capability.
#
# This is the Drive Engine: calibration scheduling, the discipline to
# maintain system accuracy over time, not just at initial setup.
# ═════════════════════════════════════════════════════════════════════════════

class ScenarioStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class CalibrationScenario:
    """A structured calibration scenario with validation criteria."""
    scenario_id: str = ""
    name: str = ""
    description: str = ""
    instructions: str = ""         # What the human needs to do
    duration_sec: float = 60.0     # How long the scenario runs
    status: ScenarioStatus = ScenarioStatus.PENDING
    validation_criteria: dict = field(default_factory=dict)  # What success looks like
    results: dict = field(default_factory=dict)               # Actual measurements
    passed: bool = False
    started_at: float = 0.0
    completed_at: float = 0.0
    zone: str = ""
    notes: str = ""


class CalibrationScenarioRunner:
    """
    AV Pattern 6: Structured calibration scenarios.

    Meta-Reasoner link: DRIVE ENGINE
    - Calibration = test track, not just baseline
    - Discipline in service of accuracy
    - Persistence mechanism for long-term reliability

    Defines a sequence of calibration scenarios that validate specific
    system capabilities. Goes beyond "empty room baseline" to include
    structured scenarios with known ground truth.

    Standard scenario set:
      1. EMPTY_BASELINE — Empty room, 60s, establishes noise floor
      2. SINGLE_WALK — One person walks through zone, validates detection
      3. SINGLE_STILL — One person sits still, validates settled detection
      4. TRANSITION — One person moves between zones, validates handoff
      5. BREATHING_GROUND_TRUTH — One person breathes normally, compare to manual count
      6. ENVIRONMENTAL — Cycle HVAC/lights to measure environmental impact

    AV origin: Test tracks with known obstacles, lane markings, and
    scenarios. You don't certify an AV by driving it in an empty parking
    lot — you run it through a structured suite of challenges.
    """

    STANDARD_SCENARIOS = [
        CalibrationScenario(
            scenario_id="empty_baseline",
            name="Empty Room Baseline",
            description="Establish noise floor with empty room",
            instructions="Ensure the zone is completely empty. No people, no pets. Stay out for 60 seconds.",
            duration_sec=60.0,
            validation_criteria={
                "max_variance": 5.0,
                "presence_should_be": False,
            },
        ),
        CalibrationScenario(
            scenario_id="single_walk",
            name="Single Person Walk-Through",
            description="Validate presence detection with single person walking",
            instructions="One person walks slowly through the zone for 30 seconds, then leaves.",
            duration_sec=45.0,
            validation_criteria={
                "min_detection_rate": 0.8,
                "motion_should_be": True,
            },
        ),
        CalibrationScenario(
            scenario_id="single_still",
            name="Single Person Stationary",
            description="Validate settled/breathing detection with stationary person",
            instructions="One person sits still in the zone for 60 seconds. Breathe normally.",
            duration_sec=60.0,
            validation_criteria={
                "min_detection_rate": 0.9,
                "motion_should_be": False,
                "breathing_expected": True,
            },
        ),
        CalibrationScenario(
            scenario_id="zone_transition",
            name="Zone Transition",
            description="Validate handoff between adjacent zones",
            instructions="Walk from this zone to the adjacent zone. Pause 5 seconds at boundary.",
            duration_sec=30.0,
            validation_criteria={
                "transition_detected": True,
                "max_gap_sec": 3.0,
            },
        ),
        CalibrationScenario(
            scenario_id="environmental",
            name="Environmental Interference",
            description="Measure HVAC/lighting impact on sensors",
            instructions="Toggle HVAC and lights during this scenario. Zone should be empty.",
            duration_sec=120.0,
            validation_criteria={
                "presence_should_be": False,
                "max_false_positive_rate": 0.05,
            },
        ),
    ]

    def __init__(self, zone: str):
        self.zone = zone
        self._scenarios: list[CalibrationScenario] = []
        self._current_index: int = -1
        self._running: bool = False
        self._data_buffer: list = []  # Sensor data collected during scenario
        self._lock = threading.Lock()

    def load_standard_scenarios(self):
        """Load the standard calibration scenario suite."""
        import copy
        self._scenarios = []
        for template in self.STANDARD_SCENARIOS:
            scenario = copy.deepcopy(template)
            scenario.zone = self.zone
            self._scenarios.append(scenario)
        log.info("Loaded %d calibration scenarios for zone %s",
                 len(self._scenarios), self.zone)

    def add_scenario(self, scenario: CalibrationScenario):
        """Add a custom scenario to the suite."""
        scenario.zone = self.zone
        self._scenarios.append(scenario)

    def start_next(self) -> Optional[CalibrationScenario]:
        """
        Start the next pending scenario. Returns the scenario or None
        if all scenarios are complete.
        """
        with self._lock:
            for i, s in enumerate(self._scenarios):
                if s.status == ScenarioStatus.PENDING:
                    s.status = ScenarioStatus.RUNNING
                    s.started_at = time.time()
                    self._current_index = i
                    self._data_buffer = []
                    self._running = True
                    log.info("Scenario started: %s — %s", s.name, s.instructions)
                    return s
        return None

    def feed_data(self, reading: dict):
        """Feed sensor data during active scenario."""
        if self._running:
            self._data_buffer.append({
                "timestamp": time.time(),
                "reading": reading,
            })

    def complete_current(self, results: dict = None) -> Optional[CalibrationScenario]:
        """
        Complete the current scenario with results.
        Validates against criteria and marks pass/fail.
        """
        with self._lock:
            if self._current_index < 0 or not self._running:
                return None

            scenario = self._scenarios[self._current_index]
            scenario.completed_at = time.time()
            scenario.results = results or {}
            scenario.results["data_points"] = len(self._data_buffer)
            scenario.results["duration_actual"] = round(
                scenario.completed_at - scenario.started_at, 1
            )

            # Validate against criteria
            scenario.passed = self._validate(scenario)
            scenario.status = (
                ScenarioStatus.COMPLETED if scenario.passed
                else ScenarioStatus.FAILED
            )
            self._running = False
            self._data_buffer = []

            log.info("Scenario %s: %s (%.1fs, %d data points)",
                     "PASSED" if scenario.passed else "FAILED",
                     scenario.name,
                     scenario.results["duration_actual"],
                     scenario.results["data_points"])
            return scenario

    def _validate(self, scenario: CalibrationScenario) -> bool:
        """Validate scenario results against criteria. Returns True if passed."""
        criteria = scenario.validation_criteria
        results = scenario.results

        for key, expected in criteria.items():
            actual = results.get(key)
            if actual is None:
                continue  # Can't validate what wasn't measured

            if isinstance(expected, bool):
                if actual != expected:
                    return False
            elif isinstance(expected, (int, float)):
                # Numeric criteria: check if actual meets the bound
                if key.startswith("min_") and actual < expected:
                    return False
                elif key.startswith("max_") and actual > expected:
                    return False

        return True

    def get_progress(self) -> dict:
        """Get calibration progress summary."""
        total = len(self._scenarios)
        completed = sum(1 for s in self._scenarios if s.status == ScenarioStatus.COMPLETED)
        failed = sum(1 for s in self._scenarios if s.status == ScenarioStatus.FAILED)
        running = sum(1 for s in self._scenarios if s.status == ScenarioStatus.RUNNING)

        return {
            "zone": self.zone,
            "total_scenarios": total,
            "completed": completed,
            "failed": failed,
            "running": running,
            "pending": total - completed - failed - running,
            "pass_rate": round(completed / max(total, 1), 2),
            "scenarios": [
                {
                    "id": s.scenario_id,
                    "name": s.name,
                    "status": s.status.value,
                    "passed": s.passed,
                }
                for s in self._scenarios
            ],
        }


# ═════════════════════════════════════════════════════════════════════════════
# META-REASONER ORCHESTRATOR — Four Components
# ═════════════════════════════════════════════════════════════════════════════
#
# The Meta-Reasoner uses the AV principles as its operating framework.
# Four components, each mapping to one or more AV patterns:
#
#   Curiosity → surfaces what is unresolved (Shadow Mode, Long Tail Logging)
#   Desire    → defines what resolution looks like (Fast/Slow Split, Degradation)
#   Drive     → sustains the system until resolution (Trust Decay, Calibration)
#   Action    → commits resolved intent as system behavior (Output Bus)
#
# System relationship:
#   Curiosity → surfaces what is unresolved
#   Desire    → defines what resolution looks like
#   Drive     → sustains the system until resolution
#   Action    → commits the resolved intent as system behavior
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class MetaReasonerState:
    """Current state of the Meta-Reasoner's four components."""
    # Curiosity state
    unresolved_count: int = 0       # Novel inputs awaiting review
    shadow_models_active: int = 0   # Shadow models currently running
    curiosity_score: float = 0.0    # 0.0 = nothing unknown, 1.0 = many unknowns

    # Desire state
    system_mode: str = "full"       # Current degradation mode
    fast_alerts_pending: int = 0    # Unresolved fast-path alerts
    goal_alignment: float = 1.0     # How well current state matches desired state

    # Drive state
    calibration_coverage: float = 0.0  # Fraction of zones calibrated
    trust_health: float = 0.0         # Average sensor trust across zones
    drive_score: float = 0.0          # System sustainability score

    # Action state
    pending_actions: int = 0        # Actions queued but not yet committed
    last_action_time: float = 0.0   # Last time the system changed state
    action_rate: float = 0.0        # Actions per minute


class MetaReasonerOrchestrator:
    """
    Orchestrates the four Meta-Reasoner components using AV principles.

    This is the integration layer — it doesn't replace the existing
    MetaReasonerService, it provides the AV-derived operational framework
    that the service uses.

    Flow per cycle:
      1. CURIOSITY: Check shadow models and long tail logger for unresolved items
      2. DESIRE: Evaluate degradation state and fast-path alert queue
      3. DRIVE: Check trust curves and calibration coverage
      4. ACTION: Commit any resolved intents to system behavior
    """

    def __init__(self):
        # AV Pattern instances
        self.trust_curve = TrustDecayCurve()
        self.shadow_runner = ShadowRunner()
        self.fast_slow = FastSlowSplit()
        self.degradation = DegradationStateMachine(self.trust_curve)
        self.long_tail = LongTailLogger()
        self._calibration_runners: dict[str, CalibrationScenarioRunner] = {}

        # Action queue — resolved intents waiting to be committed
        self._action_queue: deque = deque(maxlen=100)
        self._action_handlers: list[Callable] = []
        self._action_count = 0
        self._last_action_time = 0.0

        self._state = MetaReasonerState()
        self._lock = threading.Lock()

    def get_calibration_runner(self, zone: str) -> CalibrationScenarioRunner:
        """Get or create a calibration runner for a zone."""
        if zone not in self._calibration_runners:
            runner = CalibrationScenarioRunner(zone)
            runner.load_standard_scenarios()
            self._calibration_runners[zone] = runner
        return self._calibration_runners[zone]

    def register_action_handler(self, handler: Callable):
        """
        Register a handler for the Action component.
        When the Meta-Reasoner resolves an intent, the handler commits it.

        Typical handlers: MQTT publisher, automation trigger, state updater.
        """
        self._action_handlers.append(handler)

    def queue_action(self, action: dict):
        """
        Queue an action for the Action component to commit.
        Actions are dicts with at minimum: {type, target, payload}.
        """
        action["queued_at"] = time.time()
        self._action_queue.append(action)

    def commit_actions(self) -> list[dict]:
        """
        ACTION component: commit all queued actions.
        Returns the list of committed actions.
        """
        committed = []
        while self._action_queue:
            action = self._action_queue.popleft()
            action["committed_at"] = time.time()

            for handler in self._action_handlers:
                try:
                    handler(action)
                except Exception as e:
                    log.error("Action handler error: %s", e)
                    action["error"] = str(e)

            committed.append(action)
            self._action_count += 1
            self._last_action_time = time.time()

        return committed

    # ── Cycle ─────────────────────────────────────────────────────────────

    def run_cycle(self, zones: list[str]) -> MetaReasonerState:
        """
        Run one Meta-Reasoner cycle across all zones.

        Curiosity → Desire → Drive → Action
        """
        with self._lock:
            # ── CURIOSITY: What is unresolved? ─────────────────────────
            lt_stats = self.long_tail.get_stats()
            shadow_stats = self.shadow_runner.get_stats()

            self._state.unresolved_count = lt_stats["unreviewed"]
            self._state.shadow_models_active = len(shadow_stats)
            self._state.curiosity_score = min(
                lt_stats["unreviewed"] / max(lt_stats["total_novel_inputs"], 1),
                1.0,
            )

            # ── DESIRE: What does resolution look like? ────────────────
            for zone in zones:
                self.degradation.evaluate(zone)

            self._state.system_mode = self.degradation.current_mode.value
            recent_alerts = self.fast_slow.get_recent_alerts(10)
            self._state.fast_alerts_pending = len(recent_alerts)
            caps = self.degradation.capabilities
            self._state.goal_alignment = caps.confidence_ceiling

            # ── DRIVE: Sustain toward resolution ───────────────────────
            all_weights = []
            for zone in zones:
                weights = self.trust_curve.get_all_weights(zone)
                all_weights.extend(weights.values())

            self._state.trust_health = (
                round(sum(all_weights) / max(len(all_weights), 1), 3)
            )

            cal_total = len(self._calibration_runners)
            cal_complete = sum(
                1 for r in self._calibration_runners.values()
                if r.get_progress()["completed"] > 0
            )
            self._state.calibration_coverage = (
                round(cal_complete / max(cal_total, 1), 2)
            )
            self._state.drive_score = (
                self._state.trust_health * 0.6
                + self._state.calibration_coverage * 0.4
            )

            # ── ACTION: Commit resolved intents ────────────────────────
            committed = self.commit_actions()
            self._state.pending_actions = len(self._action_queue)
            self._state.last_action_time = self._last_action_time
            # Action rate: actions per minute over last 5 minutes
            # (simplified: just track cumulative)
            self._state.action_rate = round(self._action_count / max(
                (time.time() - self._last_action_time) / 60.0, 1.0
            ), 2) if self._last_action_time > 0 else 0.0

            return self._state

    def get_state(self) -> MetaReasonerState:
        """Get current Meta-Reasoner state."""
        with self._lock:
            return self._state

    def to_dict(self) -> dict:
        """Full diagnostic state for MQTT publishing."""
        state = self.get_state()
        return {
            "curiosity": {
                "unresolved_count": state.unresolved_count,
                "shadow_models_active": state.shadow_models_active,
                "curiosity_score": state.curiosity_score,
            },
            "desire": {
                "system_mode": state.system_mode,
                "fast_alerts_pending": state.fast_alerts_pending,
                "goal_alignment": state.goal_alignment,
            },
            "drive": {
                "calibration_coverage": state.calibration_coverage,
                "trust_health": state.trust_health,
                "drive_score": state.drive_score,
            },
            "action": {
                "pending_actions": state.pending_actions,
                "last_action_time": state.last_action_time,
                "action_rate": state.action_rate,
            },
            "trust_weights": self.trust_curve.to_dict(),
            "degradation": self.degradation.get_status(),
            "long_tail": self.long_tail.get_stats(),
            "shadows": self.shadow_runner.get_stats(),
        }
