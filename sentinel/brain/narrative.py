"""
SENTINEL Narrative Engine
==========================
Maintains the living world model — the single source of truth.

The narrative receives context messages from the fusion layer and
maintains a running causal story about the household. Every service
that needs to understand what's happening subscribes to the narrative
output, never to raw sensors.

Reasoning loop (from spec Section 2.4):
  sensor input
    → physical model update (did the environment change?)
    → narrative update (what does this mean in context?)
    → intent inference (why is this happening?)
    → specification check (does the user care about this?)
    → action (alert, adjust, anticipate, or do nothing)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from sentinel.schemas.messages import (
    NarrativeState,
    ActorState,
    Anomaly,
    ZoneOccupancy,
    ZoneState,
    SensorReading,
    HomeState,
    OccupancyState,
    AlertPriority,
)

log = logging.getLogger("sentinel.brain.narrative")


@dataclass
class NarrativeEngine:
    """
    Maintains and updates the narrative state.

    Stage 1: Basic state tracking from sensor/context messages.
    Stage 4: Full causal reasoning and intent inference.
    """

    # ── State ─────────────────────────────────────────────────────────────

    state: NarrativeState = field(default_factory=NarrativeState)

    # Name map: person_id → display name (from config known_devices)
    _name_map: dict = field(default_factory=dict)

    # Internal tracking
    _actors: dict = field(default_factory=dict)       # person_id → ActorState
    _zone_states: dict = field(default_factory=dict)   # zone → last ZoneOccupancy
    _fusion_update_times: dict = field(default_factory=dict)  # zone → last time fusion updated
    _anomalies: dict = field(default_factory=dict)     # anomaly_id → Anomaly
    _event_log: list = field(default_factory=list)     # timestamped event log
    _version: int = 0
    _start_time: float = field(default_factory=time.time)

    # ── Public API ────────────────────────────────────────────────────────

    def process_zone_occupancy(self, msg: ZoneOccupancy, from_fusion: bool = True) -> NarrativeState:
        """
        Process a zone occupancy update from the fusion layer.
        This is the primary input to the narrative during Stage 1.

        Args:
            from_fusion: True when called from the fusion context path,
                         False when called from process_sensor_reading's
                         synthetic occupancy. Only fusion updates refresh
                         the freshness timer that suppresses the raw path.

        Reasoning chain:
          1. Update zone tracking
          2. Update actor states (who moved where)
          3. Detect transitions (person left zone A, appeared in zone B)
          4. Update home-level summary
          5. Generate human-readable narrative
          6. Bump version and return new state
        """
        zone = msg.zone
        prev = self._zone_states.get(zone)
        self._zone_states[zone] = msg
        # Only real fusion updates refresh the freshness timer
        if from_fusion:
            self._fusion_update_times[zone] = time.time()

        # ── Step 1: Track occupants ───────────────────────────────────────
        for person_id in msg.occupants:
            actor = self._get_or_create_actor(person_id)

            # Detect zone transition
            if actor.current_zone != zone and actor.current_zone != "":
                actor.previous_zone = actor.current_zone
                actor.transition_time = msg.timestamp
                name = actor.display_name or self._resolve_name(person_id)
                self._log_event(f"{name} moved {actor.previous_zone} → {zone}")
                log.info(
                    "Transition: %s moved from %s → %s",
                    person_id, actor.previous_zone, zone
                )

            actor.current_zone = zone
            actor.identity_confidence = max(
                actor.identity_confidence,
                msg.confidence
            )

            # Set occupancy state from zone message
            if person_id in msg.states:
                actor.occupancy_state = msg.states[person_id]
            elif msg.occupied:
                actor.occupancy_state = OccupancyState.PRESENT.value

        # ── Step 2: Mark absent actors ────────────────────────────────────
        # If a zone reports no occupants and an actor was tracked there,
        # they've left (but we don't know where yet until another zone
        # picks them up)
        if not msg.occupied and prev and prev.occupied:
            for person_id in (prev.occupants or []):
                actor = self._actors.get(person_id)
                if actor and actor.current_zone == zone:
                    actor.occupancy_state = OccupancyState.TRANSITIONING.value
                    name = actor.display_name or self._resolve_name(person_id)
                    self._log_event(f"{name} left {zone}")
                    log.info("Actor %s left %s (transitioning)", person_id, zone)

        # ── Step 3: Update home-level summary ─────────────────────────────
        self._update_home_state()

        # ── Step 4: Generate narrative ────────────────────────────────────
        self._generate_summary(f"zone_occupancy:{zone}")

        return self.state

    def process_sensor_reading(self, msg: SensorReading) -> Optional[NarrativeState]:
        """
        Process a raw sensor reading. In Stage 1 this is used mainly for
        the CSI adapter path (before fusion service exists).

        When the fusion service is running, it publishes to context/occupancy
        with properly resolved identities. Skip synthetic occupancy here if
        fusion has recently updated this zone (within 15s) to avoid overwriting
        named occupants with "unknown_N".

        Returns updated state if the reading changed anything, None otherwise.
        """
        zone = msg.zone
        sensor = msg.sensor_type
        reading = msg.reading

        # If fusion is active for this zone, defer to its output.
        # We track fusion updates separately from synthetic occupancy built
        # by this raw sensor path — otherwise the raw path's own writes to
        # _zone_states make the freshness check always pass, defeating it.
        last_fusion = self._fusion_update_times.get(zone, 0.0)
        if (time.time() - last_fusion) < 30.0:
            # Fusion is feeding this zone — don't overwrite with raw unknowns
            return None

        # CSI presence — simple mapping to zone occupancy
        if sensor == "csi" and "present" in reading:
            present = reading.get("present", False)
            motion = reading.get("motion", False)

            # Build a synthetic ZoneOccupancy from the CSI reading
            occ = ZoneOccupancy(
                zone=zone,
                occupied=present,
                occupant_count=1 if present else 0,
                occupants=["unknown_csi"] if present else [],
                states={"unknown_csi": (
                    OccupancyState.ACTIVE.value if motion
                    else OccupancyState.PRESENT.value
                )} if present else {},
                confidence=msg.confidence,
                contributing_sensors=[sensor],
                timestamp=msg.timestamp,
            )
            return self.process_zone_occupancy(occ, from_fusion=False)

        # Radar presence
        if sensor == "radar" and "target_count" in reading:
            target_count = reading.get("target_count", 0)
            present = target_count > 0

            occ = ZoneOccupancy(
                zone=zone,
                occupied=present,
                occupant_count=target_count,
                occupants=[f"unknown_{i}" for i in range(target_count)],
                states={},
                confidence=msg.confidence,
                contributing_sensors=[sensor],
                timestamp=msg.timestamp,
            )
            return self.process_zone_occupancy(occ, from_fusion=False)

        return None

    def add_anomaly(self, anomaly: Anomaly):
        """Register an anomaly in the narrative."""
        self._anomalies[anomaly.anomaly_id] = anomaly
        log.warning("Anomaly: [%s] %s — %s", anomaly.type, anomaly.zone, anomaly.description)
        self._update_home_state()
        self._generate_summary(f"anomaly:{anomaly.anomaly_id}")

    def resolve_anomaly(self, anomaly_id: str):
        """Mark an anomaly as resolved."""
        if anomaly_id in self._anomalies:
            self._anomalies[anomaly_id].resolved = True
            self._anomalies[anomaly_id].last_updated = time.time()
            log.info("Anomaly resolved: %s", anomaly_id)
            self._update_home_state()
            self._generate_summary(f"anomaly_resolved:{anomaly_id}")

    def get_state(self) -> NarrativeState:
        """Return current narrative state (read-only snapshot)."""
        return self.state

    # ── Internal ──────────────────────────────────────────────────────────

    def _resolve_name(self, person_id: str) -> str:
        """Resolve person_id to display name via config, fallback to title-case."""
        if person_id.startswith("unknown"):
            return "Unknown"
        return self._name_map.get(person_id, person_id.replace("_", " ").title())

    def _log_event(self, text: str):
        """Append a timestamped event to the rolling log (max 50 entries)."""
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        self._event_log.append(f"[{ts}] {text}")
        if len(self._event_log) > 50:
            self._event_log = self._event_log[-50:]

    def _get_or_create_actor(self, person_id: str) -> ActorState:
        if person_id not in self._actors:
            name = self._resolve_name(person_id)
            self._actors[person_id] = ActorState(person_id=person_id, display_name=name)
            self._log_event(f"{name} detected")
            log.info("New actor tracked: %s (%s)", person_id, name)
        return self._actors[person_id]

    def _update_home_state(self):
        """Recompute home-level summary from actor states."""
        active_actors = [
            a for a in self._actors.values()
            if a.occupancy_state != OccupancyState.ABSENT.value
        ]
        known = [a for a in active_actors if not a.person_id.startswith("unknown")]
        unknown = [a for a in active_actors if a.person_id.startswith("unknown")]

        total = len(active_actors)
        self._version += 1

        if total == 0:
            home = HomeState.EMPTY.value
        elif total == 1:
            home = HomeState.OCCUPIED_SINGLE.value
        else:
            home = HomeState.OCCUPIED_MULTIPLE.value

        # Build zone state summaries
        zone_summaries = {}
        for zone, occ in self._zone_states.items():
            zone_summaries[zone] = occ.to_dict() if hasattr(occ, 'to_dict') else {}

        # Build anomaly list (active only)
        active_anomalies = [
            vars(a) if not hasattr(a, 'to_dict') else a
            for a in self._anomalies.values()
            if not a.resolved
        ]

        self.state = NarrativeState(
            home_state=home,
            total_occupants=total,
            known_occupants=len(known),
            unknown_occupants=len(unknown),
            actors=[vars(a) for a in self._actors.values()],
            zone_states=zone_summaries,
            anomalies=active_anomalies,
            narrative_version=self._version,
            brain_uptime_sec=round(time.time() - self._start_time, 1),
        )

    def _generate_summary(self, trigger: str):
        """Generate human-readable narrative summary.

        Format: one event per line with timestamp, most recent last.
        Current state header + rolling event log.
        """
        from datetime import datetime
        now = datetime.now().strftime("%H:%M:%S")
        lines = []

        # Current state: who is where
        active = [a for a in self._actors.values()
                  if a.occupancy_state != OccupancyState.ABSENT.value]

        if not active:
            lines.append(f"[{now}] Home is empty")
        else:
            # Named people get individual lines
            known = [a for a in active if not a.person_id.startswith("unknown")]
            unknown = [a for a in active if a.person_id.startswith("unknown")]

            for actor in known:
                name = actor.display_name or self._resolve_name(actor.person_id)
                state_str = actor.occupancy_state.replace("_", " ")
                zone_str = actor.current_zone.replace("_", " ")
                lines.append(f"[{now}] {name} — {zone_str}, {state_str}")

            # Collapse unknowns into a single count per zone
            if unknown:
                zone_counts: dict[str, int] = {}
                for a in unknown:
                    z = a.current_zone or "unknown"
                    zone_counts[z] = zone_counts.get(z, 0) + 1
                for z, count in zone_counts.items():
                    zone_str = z.replace("_", " ")
                    lines.append(f"[{now}] {count} unidentified presence(s) — {zone_str}")

        # Anomalies
        active_anomalies = [a for a in self._anomalies.values() if not a.resolved]
        if active_anomalies:
            lines.append(f"[{now}] {len(active_anomalies)} active anomaly(ies)")

        # Append recent event log (last 10 events)
        if self._event_log:
            lines.append("--- recent events ---")
            lines.extend(self._event_log[-10:])

        self.state.summary = "\n".join(lines)
        self.state.last_event = trigger
