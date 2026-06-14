"""
SENTINEL Reasoning Memory Schema — The Fifth Memory Tier
==========================================================
Spec Section 14.

The existing four memory tiers track what happened in the world.
Reasoning Memory tracks what the system concluded, why it concluded it,
and whether it was right.

What gets logged:
  - Decision log: what the system concluded at each reasoning step
  - Inference chain: which sensors contributed, weights, narrative context
  - Confidence at time of decision
  - Outcome validation: was it later confirmed or contradicted
  - Correction record: when wrong, what the correct interpretation was
  - Pattern of errors: recurring low-performance situations

What gets queried:
  - Meta-Reasoner reads continuously to find systematic errors
  - "Where am I systematically wrong?"
  - "What conditions predict my errors?"
  - "What would I need to get this right?"

How it feeds Meta-Reasoner:
  - Provides the evidence base for self-model updates
  - Error patterns become degradation_map entries
  - Correction records calibrate sensor reliability scores
  - Outcome validation trains confidence calibration

Storage:
  Stage 1: In-memory with JSON file persistence (simple, iteratable)
  Stage 4: SQLite with time-windowed queries and rolling aggregation
  Stage 7: Distributed across brain mesh with consensus

This module defines the schema only. The store implementation
(read/write/query) will be built in Stage 2-3.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Enums ─────────────────────────────────────────────────────────────────

class DecisionType(str, Enum):
    """What kind of decision was made."""
    PRESENCE = "presence"               # someone is/isn't here
    IDENTITY = "identity"               # who this person is
    ACTIVITY = "activity"               # what they're doing
    TRANSITION = "transition"           # person moved between zones
    ANOMALY_DETECTION = "anomaly"       # something abnormal detected
    ANOMALY_RESOLUTION = "anomaly_res"  # anomaly resolved
    INTENT = "intent"                   # why something is happening (Stage 4)
    VITAL_ASSESSMENT = "vital"          # health metric interpretation


class OutcomeStatus(str, Enum):
    """Was the decision validated?"""
    PENDING = "pending"         # not yet validated
    CONFIRMED = "confirmed"     # later evidence agrees
    CONTRADICTED = "contradicted"  # later evidence disagrees
    PARTIAL = "partial"         # partially correct
    EXPIRED = "expired"         # validation window passed, no evidence either way


class CorrectionSource(str, Enum):
    """How was a correction discovered?"""
    SENSOR_UPDATE = "sensor"        # a later sensor reading contradicted
    CROSS_SENSOR = "cross_sensor"   # another modality disagrees
    IDENTITY_UPDATE = "identity"    # face recognition corrected a CSI-only ID
    USER_FEEDBACK = "user"          # human told the system it was wrong
    TIMEOUT = "timeout"             # expected event never happened
    SELF_DETECTED = "self"          # Meta-Reasoner identified the error


# ── Decision Record ──────────────────────────────────────────────────────

@dataclass
class InferenceStep:
    """One step in the reasoning chain that led to a decision."""
    step_order: int = 0
    sensor_type: str = ""               # which sensor's data was used
    zone: str = ""
    observation: str = ""               # what the data showed
    interpretation: str = ""            # what the system concluded from it
    confidence_contribution: float = 0.0  # how much this step added to confidence
    weight_applied: float = 0.0         # fusion weight at this step


@dataclass
class DecisionRecord:
    """
    A single reasoning decision logged to reasoning memory.
    This is the fundamental unit — what was decided, why, and how confident.

    Spec 14.1: "Decision log: what the system concluded at each reasoning step"
    """
    # Identity
    decision_id: str = ""               # unique ID (e.g., "dec_1710412800_office_presence")
    decision_type: str = DecisionType.PRESENCE.value

    # What was decided
    zone: str = ""
    conclusion: str = ""                # "office is occupied by <person-id>"
    confidence: float = 0.0             # confidence at time of decision

    # Why it was decided (inference chain)
    contributing_sensors: list = field(default_factory=list)  # ["csi", "radar"]
    sensor_weights: dict = field(default_factory=dict)        # {"csi": 0.7, "radar": 0.9}
    inference_chain: list = field(default_factory=list)       # list of InferenceStep dicts
    narrative_context: str = ""         # what the narrative said at decision time

    # Environmental context (for later condition→error correlation)
    environment: dict = field(default_factory=dict)  # temp, humidity, time_of_day, etc.

    # Timing
    timestamp: float = field(default_factory=lambda: round(time.time(), 3))
    narrative_version: int = 0          # which narrative version this decision was part of

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ── Outcome Validation ───────────────────────────────────────────────────

@dataclass
class OutcomeRecord:
    """
    Validation of a previous decision — was it right?

    Spec 14.1: "Outcome validation: was the conclusion later confirmed
    or contradicted by new evidence"

    Linked to a DecisionRecord by decision_id.
    """
    decision_id: str = ""               # links to DecisionRecord
    status: str = OutcomeStatus.PENDING.value

    # What actually happened
    actual_outcome: str = ""            # "office was occupied by unknown_csi (not <person-id>)"
    actual_confidence: float = 0.0

    # How was this discovered
    correction_source: str = ""         # CorrectionSource value
    correcting_evidence: str = ""       # description of what revealed the truth

    # Impact
    confidence_error: float = 0.0       # abs(predicted_confidence - actual)
    was_correct: bool = False

    # Timing
    validation_timestamp: float = field(default_factory=lambda: round(time.time(), 3))
    latency_sec: float = 0.0           # how long between decision and validation

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ── Error Pattern ────────────────────────────────────────────────────────

@dataclass
class ErrorPattern:
    """
    A recurring error pattern identified by the Meta-Reasoner.

    Spec 14.1: "Pattern of errors: recurring situations where the system
    consistently underperforms"

    Spec 14.2: "Where am I systematically wrong?"

    Example: "Kitchen thermal readings misidentified 23 times between
    6-8pm over 6 weeks, all correlating with oven operation."
    """
    pattern_id: str = ""
    description: str = ""

    # What conditions produce this error
    zone: str = ""
    decision_type: str = ""
    time_window: str = ""               # "18:00-20:00" or "weekday_morning"
    environmental_conditions: dict = field(default_factory=dict)  # {"oven_active": True}
    affected_sensors: list = field(default_factory=list)

    # How bad is it
    occurrence_count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    error_rate: float = 0.0             # fraction of decisions in this condition that are wrong

    # What the Meta-Reasoner suggests (Spec 14.2: "What would I need to get this right?")
    suggested_correction: str = ""      # "Reduce thermal weight during cooking hours"
    correction_applied: bool = False
    correction_effective: bool = False   # did the correction actually help?

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ── Reasoning Memory Aggregate Stats ─────────────────────────────────────

@dataclass
class ReasoningMemoryStats:
    """
    Aggregate statistics across reasoning memory.
    Precomputed for fast Meta-Reasoner access.
    """
    total_decisions: int = 0
    validated_decisions: int = 0
    correct_decisions: int = 0
    contradicted_decisions: int = 0
    pending_validations: int = 0

    # Per decision type
    accuracy_by_type: dict = field(default_factory=dict)  # {DecisionType: float}

    # Per zone
    accuracy_by_zone: dict = field(default_factory=dict)  # {zone: float}

    # Per sensor
    sensor_contribution_accuracy: dict = field(default_factory=dict)  # {sensor_type: float}

    # Confidence calibration — is the system's confidence well-calibrated?
    # If it says 0.8, is it right 80% of the time?
    calibration_buckets: dict = field(default_factory=dict)  # {"0.8-0.9": {"count": N, "correct": M}}

    # Active error patterns
    active_patterns: int = 0
    corrected_patterns: int = 0

    # Temporal trends
    accuracy_trend_7d: float = 0.0      # accuracy over last 7 days
    accuracy_trend_30d: float = 0.0     # accuracy over last 30 days

    last_computed: float = 0.0

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ── Reasoning Memory Store Interface (Stage 2-3 implementation) ──────────

class ReasoningMemoryStore:
    """
    Abstract interface for reasoning memory persistence.

    Stage 1: In-memory with JSON file dump.
    Stage 4: SQLite with indexed queries.
    Stage 7: Distributed with mesh consensus.

    All methods are stubs — implementation comes with the store backend.
    """

    def record_decision(self, decision: DecisionRecord) -> str:
        """Log a decision. Returns decision_id."""
        raise NotImplementedError("Stage 2-3")

    def validate_decision(self, decision_id: str, outcome: OutcomeRecord):
        """Record the outcome of a previous decision."""
        raise NotImplementedError("Stage 2-3")

    def get_decision(self, decision_id: str) -> Optional[DecisionRecord]:
        """Retrieve a specific decision."""
        raise NotImplementedError("Stage 2-3")

    def query_decisions(
        self,
        zone: Optional[str] = None,
        decision_type: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        min_confidence: Optional[float] = None,
        max_confidence: Optional[float] = None,
        outcome_status: Optional[str] = None,
        limit: int = 100,
    ) -> list[DecisionRecord]:
        """Query decisions with filters. The Meta-Reasoner's main read path."""
        raise NotImplementedError("Stage 2-3")

    def get_error_patterns(
        self,
        zone: Optional[str] = None,
        active_only: bool = True,
    ) -> list[ErrorPattern]:
        """Get identified error patterns."""
        raise NotImplementedError("Stage 2-3")

    def record_error_pattern(self, pattern: ErrorPattern):
        """Log a newly identified error pattern."""
        raise NotImplementedError("Stage 2-3")

    def get_stats(self) -> ReasoningMemoryStats:
        """Get precomputed aggregate stats."""
        raise NotImplementedError("Stage 2-3")

    def compute_stats(self) -> ReasoningMemoryStats:
        """Recompute aggregate stats from raw data."""
        raise NotImplementedError("Stage 2-3")

    def prune(self, older_than_days: int = 90):
        """
        Roll off old validated decisions (keep error patterns and aggregates).
        Reasoning memory is permanent + rolling per spec.
        """
        raise NotImplementedError("Stage 2-3")
