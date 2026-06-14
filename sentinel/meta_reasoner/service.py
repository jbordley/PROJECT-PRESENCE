#!/usr/bin/env python3
"""
SENTINEL Meta-Reasoner
=======================
The system that watches the system. Spec Section 13.

The agent reasons about the world. The Meta-Reasoner reasons about the agent.

Three Continuous Questions (every reasoning cycle):
  1. What do I know confidently?
  2. What am I uncertain about?
  3. What would reduce that uncertainty?

This is a Stage 1 STUB — class structure and interfaces only.
Full implementation is Stage 4+.

Architecture:
  - Subscribes to sentinel/context/home/narrative (brain output)
  - Subscribes to sentinel/system/brain/status (brain health)
  - Reads from reasoning memory (when available)
  - Publishes to sentinel/system/meta/status (self-model)
  - Publishes to sentinel/system/meta/insights (curiosity-driven observations)

The Meta-Reasoner does NOT tell the agent what to conclude.
It tells the agent when its conclusions are unreliable and what would make them better.

Usage:
  python -m sentinel.meta_reasoner [--config path/to/config.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

import paho.mqtt.client as mqtt

from sentinel.config import SentinelConfig, CONFIG_PATH
from sentinel.topics import Context, System
from sentinel.schemas.messages import NarrativeState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sentinel.meta_reasoner")


# ── MQTT Topics (Meta-Reasoner specific) ──────────────────────────────────

class MetaTopics:
    """Meta-Reasoner topic namespace — extends sentinel/system/meta/..."""

    PREFIX = "sentinel/system/meta"

    @classmethod
    def status(cls) -> str:
        """Self-model and health: sentinel/system/meta/status"""
        return f"{cls.PREFIX}/status"

    @classmethod
    def insights(cls) -> str:
        """Curiosity-driven observations: sentinel/system/meta/insights"""
        return f"{cls.PREFIX}/insights"

    @classmethod
    def confidence_report(cls) -> str:
        """Per-zone confidence breakdown: sentinel/system/meta/confidence"""
        return f"{cls.PREFIX}/confidence"


# ── Uncertainty Classification (Spec 13.1) ────────────────────────────────

class UncertaintyType(str, Enum):
    """How the Meta-Reasoner classifies gaps in the world model."""
    SENSOR_GAP = "sensor_gap"               # Missing sensor coverage
    NOVEL_SITUATION = "novel_situation"      # Never seen this pattern before
    CONFLICTING_EVIDENCE = "conflicting"     # Sensors disagree
    INSUFFICIENT_HISTORY = "insufficient"    # Not enough data to judge
    DEGRADED_MODALITY = "degraded_modality"  # Known sensor in bad conditions


# ── Self-Model (Spec 13.2) ────────────────────────────────────────────────

@dataclass
class SensorReliability:
    """Tracked reliability for one sensor type in one zone."""
    sensor_type: str = ""
    zone: str = ""
    reliability_score: float = 1.0     # 0.0-1.0, learned over time
    total_readings: int = 0
    correct_readings: int = 0          # validated by outcome
    last_degraded: Optional[float] = None
    degradation_conditions: list = field(default_factory=list)  # what caused degradation
    notes: str = ""


@dataclass
class SelfModel:
    """
    The system's model of itself (Spec 13.2).

    Continuously updated. Tracks:
      - What sensors are available and how reliable they are
      - Where the system has been right and wrong (from reasoning memory)
      - What environmental conditions degrade which modalities
      - What situations consistently produce low-confidence outputs
      - What the system does not yet know how to classify
    """
    # Per-sensor, per-zone reliability tracking
    sensor_reliability: dict = field(default_factory=dict)  # (zone, sensor_type) → SensorReliability

    # Reasoning quality metrics (fed by reasoning memory, Stage 4+)
    total_decisions: int = 0
    correct_decisions: int = 0
    systematic_errors: list = field(default_factory=list)    # recurring error patterns
    failure_mode_conditions: list = field(default_factory=list)  # conditions that predict errors

    # Unknown catalog — things the system has seen but can't classify
    unknown_patterns: list = field(default_factory=list)

    # Environmental degradation model
    # Maps (condition) → [affected sensor types]
    degradation_map: dict = field(default_factory=dict)

    # Overall self-assessment
    overall_confidence: float = 0.5  # system-wide confidence in its own outputs
    last_updated: float = 0.0


# ── Confidence Assessment ─────────────────────────────────────────────────

@dataclass
class ZoneConfidence:
    """Per-zone confidence assessment from the Meta-Reasoner."""
    zone: str = ""
    confidence: float = 0.0
    active_sensors: int = 0
    expected_sensors: int = 0
    uncertainties: list = field(default_factory=list)  # list of UncertaintyType
    would_help: list = field(default_factory=list)     # what would reduce uncertainty
    notes: str = ""


@dataclass
class Insight:
    """
    A curiosity-driven observation from the Meta-Reasoner.
    Not an alert — an observation about reasoning quality.

    Example: "Kitchen thermal confidence drops 40% between 6-8pm.
    23 errors correlate with oven operation. Consider reducing thermal
    weight during cooking hours."
    """
    insight_id: str = ""
    category: str = ""          # "calibration", "pattern", "curiosity", "self_correction"
    zone: str = ""
    description: str = ""
    evidence_count: int = 0     # how many observations support this insight
    suggested_action: str = ""  # what the system could do about it
    confidence: float = 0.0
    timestamp: float = 0.0


# ── Meta-Reasoner Engine ──────────────────────────────────────────────────

class MetaReasonerEngine:
    """
    Core Meta-Reasoner logic.

    Stage 1: Stub with interface only.
    Stage 4: Full implementation with reasoning memory integration.

    The three continuous questions drive every cycle:
      1. What do I know confidently? → survey confidence across zones
      2. What am I uncertain about? → classify gaps by type
      3. What would reduce that uncertainty? → curiosity drive
    """

    def __init__(self):
        self.self_model = SelfModel()
        self._zone_confidence: dict[str, ZoneConfidence] = {}
        self._insights: list[Insight] = []
        self._last_narrative: Optional[NarrativeState] = None
        self._cycle_count: int = 0

    # ── Question 1: What do I know confidently? ───────────────────────

    def survey_confidence(self, narrative: NarrativeState) -> dict[str, ZoneConfidence]:
        """
        Survey current world model confidence levels across all zones.
        Identify high-confidence states.

        Stage 1: Returns basic confidence from narrative data.
        Stage 4: Cross-references reasoning memory for historical accuracy.
        """
        # TODO Stage 4: Implement full confidence survey
        # - Check each zone's sensor coverage vs expected
        # - Weight by historical reliability from reasoning memory
        # - Factor in environmental conditions affecting each modality
        # - Identify zones where confidence has been sustained vs oscillating
        self._last_narrative = narrative
        return self._zone_confidence

    # ── Question 2: What am I uncertain about? ────────────────────────

    def identify_uncertainties(self) -> list[dict]:
        """
        Identify gaps, ambiguities, and low-confidence states.
        Classify uncertainty by type: sensor_gap, novel_situation,
        conflicting_evidence, insufficient_history, degraded_modality.

        Stage 1: Returns empty list (no uncertainty tracking yet).
        Stage 4: Full gap analysis with typed classifications.
        """
        # TODO Stage 4: Implement uncertainty identification
        # - Zones with < 2 contributing sensors → sensor_gap
        # - Patterns not matching any learned baseline → novel_situation
        # - Sensors disagreeing on presence → conflicting_evidence
        # - New zones or new time-of-day patterns → insufficient_history
        # - Environmental conditions degrading sensors → degraded_modality
        return []

    # ── Question 3: What would reduce that uncertainty? ───────────────

    def generate_curiosity_actions(self) -> list[dict]:
        """
        The curiosity drive — targeted information seeking.
        For each uncertainty, identify what additional information
        would resolve it.

        Stage 1: Returns empty list.
        Stage 4: Generates specific sensor/calibration/training actions.

        Example actions:
          - "Adjust CSI weighting in kitchen during 6-8pm"
          - "Request additional thermal frames in hallway"
          - "Flag bathroom humidity readings for recalibration"
        """
        # TODO Stage 4: Implement curiosity action generation
        # - Map each uncertainty to a resolution strategy
        # - Prioritize by impact (which uncertainties matter most)
        # - Track which actions were taken and whether they helped
        return []

    # ── Reasoning Cycle ───────────────────────────────────────────────

    def run_cycle(self, narrative: NarrativeState) -> Optional[Insight]:
        """
        Execute one meta-reasoning cycle. Called on each narrative update.

        Returns an Insight if one was generated, None otherwise.

        Stage 1: Logs cycle count, no real analysis.
        Stage 4: Full three-question cycle with reasoning memory.
        """
        self._cycle_count += 1
        self._last_narrative = narrative

        # The three questions
        confidence = self.survey_confidence(narrative)
        uncertainties = self.identify_uncertainties()
        actions = self.generate_curiosity_actions()

        # Stage 4: Feed results to reasoning memory
        # Stage 4: Generate insight if pattern detected
        # Stage 4: Update self-model based on outcomes

        if self._cycle_count % 100 == 0:
            log.info(
                "Meta-Reasoner cycle %d — zones=%d, narrative_v=%d",
                self._cycle_count,
                len(narrative.zone_states) if narrative.zone_states else 0,
                narrative.narrative_version,
            )

        return None

    # ── Self-Model Access ─────────────────────────────────────────────

    def get_self_model(self) -> SelfModel:
        """Return current self-model snapshot."""
        self.self_model.last_updated = time.time()
        return self.self_model

    def get_status_payload(self) -> dict:
        """Build MQTT status payload."""
        model = self.self_model
        return {
            "status": "online",
            "cycle_count": self._cycle_count,
            "overall_confidence": model.overall_confidence,
            "total_decisions_tracked": model.total_decisions,
            "unknown_patterns": len(model.unknown_patterns),
            "systematic_errors": len(model.systematic_errors),
            "timestamp": round(time.time(), 3),
        }

    # ── Reasoning Memory Interface (Stage 4) ──────────────────────────

    def record_decision(self, decision: dict):
        """
        Record a reasoning decision for later outcome validation.
        Stub — full implementation requires reasoning memory schema.

        Args:
            decision: {
                "decision_id": str,
                "zone": str,
                "conclusion": str,          # what the system decided
                "confidence": float,         # how confident it was
                "contributing_sensors": [],   # what evidence it used
                "inference_chain": [],        # reasoning steps
                "timestamp": float,
            }
        """
        # TODO Stage 4: Write to reasoning memory store
        pass

    def validate_outcome(self, decision_id: str, actual_outcome: dict):
        """
        Validate a previous decision against what actually happened.
        This is how the system learns whether it was right.

        Args:
            decision_id: ID from record_decision
            actual_outcome: {
                "correct": bool,
                "actual_state": str,
                "correction_source": str,   # what revealed the truth
            }
        """
        # TODO Stage 4: Update reasoning memory, feed self-model
        pass


# ── Meta-Reasoner Service ─────────────────────────────────────────────────

class MetaReasonerService:
    """
    MQTT service wrapper for the Meta-Reasoner.

    Subscribes to narrative output, runs meta-reasoning cycles,
    publishes self-model and insights.

    Stage 1: Connects, subscribes, logs. No real analysis.
    Stage 4: Full cycle with reasoning memory integration.
    """

    def __init__(self, config: SentinelConfig):
        self.config = config
        self.engine = MetaReasonerEngine()
        self.running = False
        self._start_time = time.time()
        self._lock = threading.Lock()
        self._last_status_publish = 0.0

        # MQTT client
        client_id = f"{config.mqtt.client_id_prefix}-meta-reasoner"
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

        # Last will
        self.mqttc.will_set(
            MetaTopics.status(),
            json.dumps({"status": "offline", "timestamp": round(time.time(), 3)}),
            qos=1,
            retain=True,
        )

        if config.mqtt.username:
            self.mqttc.username_pw_set(config.mqtt.username, config.mqtt.password)

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed: rc=%d", rc)
            return

        log.info("MQTT connected")

        # Subscribe to narrative (the brain's output)
        client.subscribe(Context.narrative(), qos=1)
        log.info("Subscribed: %s", Context.narrative())

        # Subscribe to brain status
        client.subscribe(System.brain_status(), qos=1)
        log.info("Subscribed: %s", System.brain_status())

        # Publish online status
        client.publish(
            MetaTopics.status(),
            json.dumps(self.engine.get_status_payload()),
            qos=1,
            retain=True,
        )

        log.info("Meta-Reasoner service online (Stage 1 stub)")

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.warning("MQTT disconnected: rc=%d", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        topic = msg.topic

        with self._lock:
            # Narrative update — run meta-reasoning cycle
            if topic == Context.narrative():
                self._handle_narrative(payload)

            # Brain status — track brain health
            elif topic == System.brain_status():
                self._handle_brain_status(payload)

    def _handle_narrative(self, payload: dict):
        """Process a narrative update — trigger meta-reasoning cycle."""
        try:
            narrative = NarrativeState.from_dict(payload)
        except Exception as e:
            log.debug("Failed to parse narrative: %s", e)
            return

        insight = self.engine.run_cycle(narrative)

        if insight:
            self.mqttc.publish(
                MetaTopics.insights(),
                json.dumps({
                    "insight_id": insight.insight_id,
                    "category": insight.category,
                    "zone": insight.zone,
                    "description": insight.description,
                    "suggested_action": insight.suggested_action,
                    "confidence": insight.confidence,
                    "timestamp": insight.timestamp,
                }),
                qos=0,
            )

    def _handle_brain_status(self, payload: dict):
        """Track brain health for Meta-Reasoner's self-model."""
        status = payload.get("status", "unknown")
        if status != "online":
            log.warning("Brain status: %s", status)
        # TODO Stage 4: Factor brain health into self-model

    def _publish_status(self):
        """Periodic status heartbeat."""
        self.mqttc.publish(
            MetaTopics.status(),
            json.dumps(self.engine.get_status_payload()),
            qos=1,
            retain=True,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────

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
        log.info("Meta-Reasoner service started")

        try:
            while self.running:
                now = time.time()
                if now - self._last_status_publish >= 30.0:
                    self._last_status_publish = now
                    self._publish_status()
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if not self.running:
            return
        self.running = False
        log.info("Meta-Reasoner stopping...")
        self.mqttc.publish(
            MetaTopics.status(),
            json.dumps({"status": "offline", "timestamp": round(time.time(), 3)}),
            qos=1,
            retain=True,
        )
        self.mqttc.loop_stop()
        self.mqttc.disconnect()
        log.info("Meta-Reasoner stopped")


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SENTINEL Meta-Reasoner")
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

    svc = MetaReasonerService(config)

    def handle_signal(signum, frame):
        svc.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    svc.start()


if __name__ == "__main__":
    main()
