"""
SENTINEL Correlation Engine
=============================
Consumes SensorAssertions from all sensors, matches them against correlation
rules, and produces CorrelatedEntities — the unified picture of who/what
is in each zone.

This is the intelligence core of the fusion system. Raw sensor data becomes
assertions (via adapters), assertions become correlated entities (here),
and correlated entities become narrative (via the brain).

Data flow:
  Sensor Adapters → SensorAssertions → [Correlation Engine] → CorrelatedEntities
                                              ↓
                                       Anomaly Detection
                                       Threat Assessment
                                       Activity Inference

Design:
  - Per-zone assertion buffer (circular, time-windowed)
  - Each correlation cycle:
      1. Collect all non-expired assertions for the zone
      2. Group by entity (spatial proximity + identity matching)
      3. Apply correlation rules
      4. Produce CorrelatedEntities
      5. Detect anomalies from rule evaluation
      6. Publish results

  - Entity matching heuristics:
      - Same person_id from different sensors → same entity
      - Spatial proximity within threshold → candidate match
      - Temporal overlap → strengthens match
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from sentinel.schemas.assertions import (
    SensorAssertion,
    CorrelatedEntity,
    CorrelationRule,
    CORRELATION_RULES,
    AssertionType,
    SensorSource,
    EntityType,
    SpatialRef,
)

log = logging.getLogger("sentinel.fusion.correlation")


# ── Configuration ────────────────────────────────────────────────────────

ASSERTION_WINDOW_SEC = 60.0     # Keep assertions for this long
MAX_ASSERTIONS_PER_ZONE = 500   # Cap to prevent memory growth
SPATIAL_MATCH_THRESHOLD_M = 3.0 # Two detections within 3m = candidate match
STALE_ENTITY_SEC = 45.0         # Entity with no recent assertions = stale
PRUNE_ENTITY_SEC = 120.0        # Remove stale entities after 2 min


# ── Zone Assertion Buffer ────────────────────────────────────────────────

@dataclass
class ZoneAssertionBuffer:
    """Holds recent assertions for one zone. Time-windowed circular buffer."""
    zone: str = ""
    assertions: list = field(default_factory=list)  # list[SensorAssertion]

    # Current correlated entities
    entities: dict = field(default_factory=dict)  # entity_id → CorrelatedEntity

    # Stats
    total_assertions_received: int = 0
    total_correlations_run: int = 0
    last_correlation_time: float = 0.0

    def add_assertion(self, assertion: SensorAssertion):
        """Add an assertion to the buffer, pruning expired ones."""
        self.assertions.append(assertion)
        self.total_assertions_received += 1

        # Prune expired
        now = time.time()
        self.assertions = [
            a for a in self.assertions
            if (now - a.timestamp) < ASSERTION_WINDOW_SEC
        ]

        # Cap size
        if len(self.assertions) > MAX_ASSERTIONS_PER_ZONE:
            self.assertions = self.assertions[-MAX_ASSERTIONS_PER_ZONE:]

    def get_active(self) -> list[SensorAssertion]:
        """Get all non-expired assertions."""
        now = time.time()
        return [a for a in self.assertions if not a.is_expired]


# ── Correlation Engine ───────────────────────────────────────────────────

class CorrelationEngine:
    """Matches assertions across sensors to build correlated entities.

    Usage:
        engine = CorrelationEngine()
        engine.ingest(assertion)  # called by each sensor adapter
        entities = engine.correlate("office")  # called each fusion cycle

    The engine maintains per-zone buffers and entity state.
    """

    def __init__(self, rules: list[CorrelationRule] | None = None):
        self._zones: dict[str, ZoneAssertionBuffer] = {}
        self._rules = sorted(
            rules or CORRELATION_RULES,
            key=lambda r: r.priority,
            reverse=True,  # highest priority first
        )
        log.info("CorrelationEngine initialized with %d rules", len(self._rules))

    def ingest(self, assertion: SensorAssertion):
        """Ingest a single assertion from any sensor.

        This is called by sensor adapters each time they produce an assertion.
        Thread-safe from the perspective of the MQTT callback thread.
        """
        zone = assertion.zone
        if zone not in self._zones:
            self._zones[zone] = ZoneAssertionBuffer(zone=zone)
        self._zones[zone].add_assertion(assertion)

    def ingest_batch(self, assertions: list[SensorAssertion]):
        """Ingest multiple assertions at once (e.g., from a single scan cycle)."""
        for a in assertions:
            self.ingest(a)

    def correlate(self, zone: str) -> dict:
        """Run correlation for a zone. Returns the full correlation result.

        Returns:
            {
                "entities": [CorrelatedEntity.to_dict(), ...],
                "anomalies": [{"type": ..., "description": ..., ...}, ...],
                "stats": {"assertion_count": N, "entity_count": N, ...},
            }
        """
        if zone not in self._zones:
            return {"entities": [], "anomalies": [], "stats": {}}

        buf = self._zones[zone]
        active = buf.get_active()
        buf.total_correlations_run += 1
        buf.last_correlation_time = time.time()

        if not active:
            # No active assertions — mark all entities stale
            self._age_entities(buf)
            return self._build_result(buf, [])

        # ── Step 1: Group assertions by entity ──
        groups = self._group_assertions(active)

        # ── Step 2: Build/update CorrelatedEntities ──
        anomalies = []
        seen_entity_ids = set()

        for group_key, group_assertions in groups.items():
            entity = self._resolve_entity(buf, group_key, group_assertions)
            seen_entity_ids.add(entity.entity_id)

            # ── Step 3: Apply correlation rules ──
            rule_anomalies = self._apply_rules(entity, group_assertions, active)
            anomalies.extend(rule_anomalies)

        # ── Step 4: Age unseen entities ──
        self._age_entities(buf, seen_entity_ids)

        return self._build_result(buf, anomalies)

    # ── Entity Resolution ────────────────────────────────────────────────

    def _group_assertions(self, assertions: list[SensorAssertion]) -> dict:
        """Group assertions into clusters that likely refer to the same entity.

        Grouping heuristics (in priority order):
          1. Same person_id → same entity (strongest signal)
          2. Same device_mac → same entity
          3. Spatial proximity within threshold → candidate (weaker)
        """
        groups: dict[str, list[SensorAssertion]] = defaultdict(list)

        for a in assertions:
            # Priority 1: Known person identity
            if a.person_id:
                groups[f"person:{a.person_id}"].append(a)
                continue

            # Priority 2: Device MAC
            if a.device_mac:
                groups[f"mac:{a.device_mac}"].append(a)
                continue

            # Priority 3: Non-identity assertions (CSI, thermal, radar)
            # These go into a "zone-level" bucket for spatial matching
            groups[f"anon:{a.source.value}:{a.assertion_type.value}"].append(a)

        return dict(groups)

    def _resolve_entity(self, buf: ZoneAssertionBuffer, group_key: str,
                        assertions: list[SensorAssertion]) -> CorrelatedEntity:
        """Resolve a group of assertions into a CorrelatedEntity.

        Creates a new entity or updates an existing one.
        """
        now = time.time()

        # Check if we already track this entity
        entity = buf.entities.get(group_key)
        if entity is None:
            entity = CorrelatedEntity(
                entity_id=group_key,
                zone=buf.zone,
                first_seen=now,
            )
            buf.entities[group_key] = entity

        # Aggregate data from all assertions in this group
        sources = set()
        best_identity_conf = 0.0
        best_presence_conf = 0.0
        best_spatial = None
        best_spatial_accuracy = float('inf')

        for a in assertions:
            sources.add(a.source.value)

            # Identity
            if a.assertion_type == AssertionType.IDENTITY:
                if a.confidence > best_identity_conf:
                    best_identity_conf = a.confidence
                    entity.person_id = a.person_id
                    entity.person_name = a.person_name
                    entity.entity_type = EntityType.PERSON if a.person_id else entity.entity_type

            # Presence
            if a.assertion_type == AssertionType.PRESENCE:
                best_presence_conf = max(best_presence_conf, a.confidence)

            # Spatial — keep the most accurate
            if a.spatial:
                accuracy = a.spatial.accuracy_m or 5.0  # default 5m if unspecified
                if accuracy < best_spatial_accuracy:
                    best_spatial_accuracy = accuracy
                    best_spatial = a.spatial

            # Motion
            if a.assertion_type == AssertionType.MOTION:
                if a.direction:
                    entity.motion_state = a.direction
                elif a.speed_mms is not None:
                    entity.motion_state = "stationary" if a.speed_mms < 50 else "moving"

            # Vitals
            if a.assertion_type == AssertionType.VITALS:
                if a.heart_rate_bpm is not None:
                    entity.heart_rate_bpm = a.heart_rate_bpm
                if a.breathing_rate_bpm is not None:
                    entity.breathing_rate_bpm = a.breathing_rate_bpm
                entity.vitals_confidence = max(entity.vitals_confidence, a.confidence)

            # Duration from longest-tracking assertion
            if a.duration_sec > entity.duration_sec:
                entity.duration_sec = a.duration_sec

        # Update entity
        entity.supporting_sources = sorted(sources)
        entity.assertion_count = len(assertions)
        entity.last_updated = now
        entity.identity_confidence = best_identity_conf
        entity.presence_confidence = best_presence_conf
        entity.best_position = best_spatial
        entity.position_confidence = min(1.0, 1.0 / max(best_spatial_accuracy, 0.5)) if best_spatial else 0.0

        # Multi-sensor confirmation status
        if len(sources) >= 2:
            entity.status = "confirmed"
        elif len(sources) == 1:
            entity.status = "tentative"

        # Duration update
        entity.duration_sec = now - entity.first_seen

        return entity

    # ── Rule Evaluation ──────────────────────────────────────────────────

    def _apply_rules(self, entity: CorrelatedEntity,
                     entity_assertions: list[SensorAssertion],
                     all_assertions: list[SensorAssertion]) -> list[dict]:
        """Apply correlation rules to an entity and return any anomalies."""
        anomalies = []

        # Index assertions by (source, type) at both entity and zone level
        all_by_key = defaultdict(list)
        for a in all_assertions:
            all_by_key[(a.source.value, a.assertion_type.value)].append(a)

        entity_by_key = defaultdict(list)
        for a in entity_assertions:
            entity_by_key[(a.source.value, a.assertion_type.value)].append(a)

        for rule in self._rules:
            # For confidence-boosting and identity rules, require at least one
            # condition to match at the ENTITY level (not just zone-wide).
            # This prevents anonymous entities from inheriting identity boosts.
            requires_entity_match = rule.action in ("boost_confidence", "infer_activity")

            requires_met = True
            has_entity_match = False
            for req in rule.requires:
                key = (req["source"], req["type"])
                if entity_by_key[key]:
                    has_entity_match = True
                elif all_by_key[key]:
                    pass  # Zone-level match only
                else:
                    requires_met = False
                    break

            if not requires_met:
                continue

            # Skip if rule needs entity-level match but only has zone-level
            if requires_entity_match and not has_entity_match:
                continue

            # Check excludes (negative conditions) — zone-wide is correct here
            excluded = False
            for exc in rule.excludes:
                key = (exc["source"], exc["type"])
                if all_by_key[key]:
                    excluded = True
                    break

            if excluded:
                continue

            # Rule fires — apply action
            action = rule.action
            params = rule.action_params

            if action == "boost_confidence":
                if "identity_confidence" in params:
                    entity.identity_confidence = max(entity.identity_confidence, params["identity_confidence"])
                if "presence_confidence" in params:
                    entity.presence_confidence = max(entity.presence_confidence, params["presence_confidence"])
                if "presence_confidence_boost" in params:
                    entity.presence_confidence = min(1.0, entity.presence_confidence + params["presence_confidence_boost"])
                if "status" in params:
                    entity.status = params["status"]

            elif action == "flag_anomaly":
                anomaly = {
                    "rule_id": rule.rule_id,
                    "type": params.get("anomaly_type", rule.rule_id),
                    "description": params.get("description", rule.description),
                    "entity_id": entity.entity_id,
                    "zone": entity.zone,
                    "timestamp": time.time(),
                }
                anomalies.append(anomaly)
                entity.anomalies.append(anomaly["type"])

                if "threat_boost" in params:
                    current = {"none": 0, "low": 1, "medium": 2, "high": 3}
                    reverse = {v: k for k, v in current.items()}
                    new_level = min(current.get(entity.threat_level, 0) + params["threat_boost"], 3)
                    entity.threat_level = reverse.get(new_level, "medium")
                    entity.threat_reasons.append(rule.name)

            elif action == "escalate_threat":
                if "threat_level" in params:
                    entity.threat_level = params["threat_level"]
                    entity.threat_reasons.append(params.get("description", rule.name))

            elif action == "infer_activity":
                if "activity_hint" in params:
                    entity.activity_hint = params["activity_hint"]
                if "motion_state" in params:
                    entity.motion_state = params["motion_state"]

            elif action == "merge_entities":
                # Future: merge two entity groups into one
                pass

            log.debug("Rule '%s' fired for entity %s in %s",
                      rule.name, entity.entity_id, entity.zone)

        return anomalies

    # ── Entity Lifecycle ─────────────────────────────────────────────────

    def _age_entities(self, buf: ZoneAssertionBuffer,
                      seen_ids: set | None = None):
        """Mark entities as stale or remove them if too old."""
        now = time.time()
        to_remove = []

        for eid, entity in buf.entities.items():
            if seen_ids and eid in seen_ids:
                continue  # Still active

            age = now - entity.last_updated
            if age > PRUNE_ENTITY_SEC:
                to_remove.append(eid)
            elif age > STALE_ENTITY_SEC:
                entity.status = "stale"

        for eid in to_remove:
            del buf.entities[eid]
            log.debug("Pruned stale entity: %s in %s", eid, buf.zone)

    # ── Result Building ──────────────────────────────────────────────────

    def _build_result(self, buf: ZoneAssertionBuffer,
                      anomalies: list[dict]) -> dict:
        """Build the correlation result for consumption by the brain."""
        entities = [e.to_dict() for e in buf.entities.values()
                    if e.status != "stale"]

        # Separate known persons, unknown humans, and devices
        known_persons = [e for e in entities if e.get("person_id")]
        unknown_entities = [e for e in entities if not e.get("person_id")]

        return {
            "zone": buf.zone,
            "entities": entities,
            "known_persons": known_persons,
            "unknown_entities": unknown_entities,
            "anomalies": anomalies,
            "stats": {
                "active_assertions": len(buf.get_active()),
                "total_assertions": buf.total_assertions_received,
                "entity_count": len(buf.entities),
                "known_count": len(known_persons),
                "unknown_count": len(unknown_entities),
                "correlation_count": buf.total_correlations_run,
            },
            "timestamp": time.time(),
        }

    # ── Public API ───────────────────────────────────────────────────────

    def get_entities(self, zone: str) -> list[CorrelatedEntity]:
        """Get current correlated entities for a zone."""
        if zone not in self._zones:
            return []
        return [e for e in self._zones[zone].entities.values()
                if e.status != "stale"]

    def get_entity(self, zone: str, entity_id: str) -> Optional[CorrelatedEntity]:
        """Get a specific entity by ID."""
        if zone not in self._zones:
            return None
        return self._zones[zone].entities.get(entity_id)

    def get_anomalies(self, zone: str) -> list[dict]:
        """Get current anomalies for a zone (from last correlation cycle)."""
        # Anomalies are transient — recalculated each correlation cycle
        # For persistent anomaly tracking, the brain handles it
        if zone not in self._zones:
            return []
        entities = self._zones[zone].entities.values()
        return [
            {"entity_id": e.entity_id, "anomalies": e.anomalies,
             "threat_level": e.threat_level, "threat_reasons": e.threat_reasons}
            for e in entities if e.anomalies
        ]

    def get_stats(self) -> dict:
        """Get engine-wide stats."""
        return {
            zone: {
                "entities": len(buf.entities),
                "active_assertions": len(buf.get_active()),
                "total_received": buf.total_assertions_received,
                "correlations_run": buf.total_correlations_run,
            }
            for zone, buf in self._zones.items()
        }
