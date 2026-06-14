#!/usr/bin/env python3
"""
Project Presence — CSI Aggregator Bridge
=========================================
Runs on <keep-host> (Jetson Orin Nano, <jetson-ip>).

Listens for UDP binary CSI frames from ESP32-S3 nodes (Phase A firmware),
parses them, computes presence detection (Phase B) and breathing rate
extraction (Phase C), and publishes results to Mosquitto MQTT.

Frame format (from Phase A firmware):
  Offset  Size  Field
    0       4   Magic: 0xC5110001
    4       1   Node ID (1-4)
    5       1   Number of antennas
    6       2   Number of subcarriers (LE u16)
    8       4   Sequence number (LE u32)
   12       1   RSSI (i8)
   13       1   Noise floor (i8)
   14       2   Reserved
   16       N   Amplitude bytes (uint8, per subcarrier per antenna)
   16+N     N   Phase bytes (int8 scaled, per subcarrier per antenna)

MQTT topics published:
  home/csi/{node_id}/stats       — raw CSI stats (RSSI, noise, frame rate, amplitude summary)
  home/csi/{node_id}/status      — online/offline (retained)
  home/csi/{node_id}/calibration — calibration progress/baseline stats (retained)
  home/presence/{zone}           — presence + motion events (once zone assigned)
  home/breathing/{zone}          — breathing rate BPM (once zone assigned, Phase C)
  home/csi/discovery             — new node announcements

Zone assignment via MQTT:
  Publish to  home/csi/{node_id}/zone/set  with payload like "desk"
  Bridge will map that node to zone and start publishing to home/presence/{zone}

Recalibrate via MQTT:
  Publish to  home/csi/{node_id}/calibrate  (any payload)
  Restarts 60s baseline capture for that node

Calibration system:
  On first frame from each node, a 60-second baseline capture begins automatically.
  During this window, the room should be empty (no presence). The bridge collects
  per-subcarrier amplitude statistics, then computes:
    - Baseline mean and std per subcarrier
    - 3-sigma presence thresholds per subcarrier
    - 5-sigma motion thresholds per subcarrier
  After calibration, live readings are EMA-filtered and compared against per-channel
  thresholds. A slow-drift EMA updates the baseline over time to handle environmental
  changes (temperature, humidity, furniture moves), but only when the room appears empty.

Usage:
  python3 csi_bridge.py [--port 5005] [--mqtt-host 127.0.0.1] [--mqtt-port 1883]
                        [--calibration-duration 60]

Systemd:
  See bridge/csi_bridge.service
"""

import argparse
import copy
import json
import logging
import os
import signal
import socket
import sqlite3
import struct
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import paho.mqtt.client as mqtt

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("csi-bridge")

# ── Constants ────────────────────────────────────────────────────────────────

FRAME_MAGIC = 0xC5110001
HEADER_SIZE = 16
MAX_FRAME_SIZE = 2048  # generous upper bound

# Calibration
CALIBRATION_DURATION_SEC = 60      # baseline capture window (empty room)
CALIBRATION_MIN_FRAMES = 200       # minimum frames before baseline is valid
SIGMA_MULTIPLIER = 3.0             # 3-sigma threshold for presence detection
EMA_ALPHA = 0.05                   # EMA smoothing factor for live readings (lower = smoother)
BASELINE_DRIFT_ALPHA = 0.001       # very slow EMA for baseline drift correction
BASELINE_DRIFT_GUARD = 0.3        # only drift-update when variance < 30% of threshold
CALIBRATION_PUBLISH_INTERVAL = 5.0 # seconds between calibration status publishes

# Phase B — presence detection
AMPLITUDE_HISTORY_LEN = 100        # ~5s at 20 Hz
PRESENCE_VARIANCE_THRESHOLD = 15.0 # fallback: used only before calibration completes
MOTION_VARIANCE_THRESHOLD = 80.0   # high variance = active motion
MOTION_SIGMA_MULTIPLIER = 5.0      # motion threshold = 5x the 3-sigma presence threshold
PRESENCE_HOLD_TIME = 10.0          # seconds to hold presence after variance drops

# Phase C — breathing extraction
BREATHING_WINDOW_SEC = 30          # seconds of data for FFT
BREATHING_MIN_HZ = 0.1            # 6 breaths/min
BREATHING_MAX_HZ = 0.5            # 30 breaths/min
BREATHING_MIN_CONFIDENCE = 0.3    # peak must be this fraction of total power

# Stats / publishing intervals
STATS_INTERVAL = 2.0              # seconds between CSI stats publishes
PRESENCE_INTERVAL = 1.0           # seconds between presence publishes
BREATHING_INTERVAL = 5.0          # seconds between breathing publishes

# Persistence
DEFAULT_DB_PATH = "/var/lib/sentinel/csi_calibration.db"  # Production: <broker-host> SQLite


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class CSIFrame:
    """Parsed CSI frame from an ESP32-S3 node."""
    node_id: int
    n_antennas: int
    n_subcarriers: int
    seq: int
    rssi: int
    noise_floor: int
    amplitudes: np.ndarray   # shape: (n_antennas, n_subcarriers)
    phases: np.ndarray       # shape: (n_antennas, n_subcarriers)
    timestamp: float = field(default_factory=time.time)


@dataclass
class CalibrationState:
    """Per-node calibration baseline and thresholds."""
    calibrated: bool = False
    calibrating: bool = False
    cal_start_time: float = 0.0
    cal_frames: int = 0
    cal_duration: float = CALIBRATION_DURATION_SEC

    # Accumulation buffers — filled during calibration window
    # Each entry is a per-subcarrier amplitude vector (antenna 0)
    cal_amp_buffer: list = field(default_factory=list)

    # Computed baseline (post-calibration)
    baseline_mean: Optional[np.ndarray] = None    # shape: (n_subcarriers,)
    baseline_std: Optional[np.ndarray] = None     # shape: (n_subcarriers,)
    threshold_presence: Optional[np.ndarray] = None  # mean + 3σ per subcarrier
    threshold_motion: Optional[np.ndarray] = None    # mean + 5σ per subcarrier

    # EMA-smoothed live amplitude (per subcarrier)
    ema_amplitude: Optional[np.ndarray] = None    # shape: (n_subcarriers,)

    # Slow-drift baseline (updated only when room appears empty)
    drift_baseline: Optional[np.ndarray] = None   # shape: (n_subcarriers,)
    drift_std: Optional[np.ndarray] = None         # shape: (n_subcarriers,)


@dataclass
class NodeState:
    """Per-node tracking state."""
    node_id: int
    zone: Optional[str] = None
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    frame_count: int = 0
    last_seq: int = 0

    # Calibration
    calibration: CalibrationState = field(default_factory=CalibrationState)

    # Amplitude history for variance computation (per subcarrier mean)
    amp_history: deque = field(default_factory=lambda: deque(maxlen=AMPLITUDE_HISTORY_LEN))

    # Breathing extraction — longer history of mean amplitude per frame
    breathing_history: deque = field(default_factory=lambda: deque(maxlen=600))  # 30s at 20Hz
    breathing_timestamps: deque = field(default_factory=lambda: deque(maxlen=600))

    # Presence state
    present: bool = False
    motion: bool = False
    last_presence_time: float = 0.0
    last_variance: float = 0.0

    # Breathing state
    breathing_bpm: Optional[float] = None
    breathing_confidence: float = 0.0

    # Stats
    last_stats_publish: float = 0.0
    last_presence_publish: float = 0.0
    last_breathing_publish: float = 0.0
    last_calibration_publish: float = 0.0


# ── Calibration Persistence (SQLite) ─────────────────────────────────────

class CalibrationStore:
    """
    SQLite persistence for calibration baselines. Survives process restarts.

    On <broker-host> (Raspberry Pi 4) this lives at /var/lib/sentinel/csi_calibration.db.
    Each node's calibration baseline (mean, std, thresholds) is stored as
    numpy arrays serialized to bytes. On startup, the bridge loads the most
    recent baseline per node and skips the 60-second calibration window if
    the baseline is fresh enough (default: 24 hours).

    Schema:
      calibrations(
        node_id       INTEGER,
        calibrated_at REAL,         -- epoch timestamp
        n_subcarriers INTEGER,
        baseline_mean BLOB,         -- numpy float32 array
        baseline_std  BLOB,
        threshold_presence BLOB,
        threshold_motion   BLOB,
        cal_frames    INTEGER,
        metadata      TEXT          -- JSON: RSSI, noise floor, etc.
      )
    """

    # Baselines older than this are considered stale and re-calibrated
    FRESHNESS_HOURS = 24

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._ensure_dir()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._create_tables()
        log.info("CalibrationStore: %s", db_path)

    def _ensure_dir(self):
        dirpath = os.path.dirname(self.db_path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)

    def _create_tables(self):
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS calibrations (
                    node_id         INTEGER NOT NULL,
                    calibrated_at   REAL NOT NULL,
                    n_subcarriers   INTEGER NOT NULL,
                    baseline_mean   BLOB NOT NULL,
                    baseline_std    BLOB NOT NULL,
                    threshold_presence BLOB NOT NULL,
                    threshold_motion   BLOB NOT NULL,
                    cal_frames      INTEGER NOT NULL,
                    metadata        TEXT,
                    PRIMARY KEY (node_id)
                )
            """)
            self.conn.commit()

    def save(self, node_id: int, cal: 'CalibrationState', metadata: dict = None):
        """Persist a completed calibration baseline for a node."""
        if not cal.calibrated or cal.baseline_mean is None:
            return

        with self._lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO calibrations
                    (node_id, calibrated_at, n_subcarriers,
                     baseline_mean, baseline_std,
                     threshold_presence, threshold_motion,
                     cal_frames, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                node_id,
                time.time(),
                len(cal.baseline_mean),
                cal.baseline_mean.astype(np.float32).tobytes(),
                cal.baseline_std.astype(np.float32).tobytes(),
                cal.threshold_presence.astype(np.float32).tobytes(),
                cal.threshold_motion.astype(np.float32).tobytes(),
                cal.cal_frames,
                json.dumps(metadata) if metadata else None,
            ))
            self.conn.commit()

        log.info("Node %d: calibration saved to SQLite (%d subcarriers, %d frames)",
                 node_id, len(cal.baseline_mean), cal.cal_frames)

    def load(self, node_id: int) -> Optional['CalibrationState']:
        """
        Load the most recent calibration for a node.
        Returns a populated CalibrationState if a fresh baseline exists, else None.
        """
        with self._lock:
            row = self.conn.execute("""
                SELECT calibrated_at, n_subcarriers,
                       baseline_mean, baseline_std,
                       threshold_presence, threshold_motion,
                       cal_frames
                FROM calibrations WHERE node_id = ?
            """, (node_id,)).fetchone()

        if row is None:
            return None

        calibrated_at, n_sub, mean_b, std_b, tp_b, tm_b, frames = row

        # Check freshness
        age_hours = (time.time() - calibrated_at) / 3600.0
        if age_hours > self.FRESHNESS_HOURS:
            log.info("Node %d: stored baseline is %.1f hours old (stale), will re-calibrate",
                     node_id, age_hours)
            return None

        cal = CalibrationState()
        cal.calibrated = True
        cal.calibrating = False
        cal.cal_frames = frames
        cal.baseline_mean = np.frombuffer(mean_b, dtype=np.float32).copy()
        cal.baseline_std = np.frombuffer(std_b, dtype=np.float32).copy()
        cal.threshold_presence = np.frombuffer(tp_b, dtype=np.float32).copy()
        cal.threshold_motion = np.frombuffer(tm_b, dtype=np.float32).copy()
        cal.drift_baseline = cal.baseline_mean.copy()
        cal.drift_std = cal.baseline_std.copy()
        cal.ema_amplitude = cal.baseline_mean.copy()

        log.info("Node %d: loaded calibration from SQLite (%.1f hours old, %d subcarriers, %d frames)",
                 node_id, age_hours, n_sub, frames)
        return cal

    def list_nodes(self) -> list[dict]:
        """List all stored calibrations with metadata."""
        with self._lock:
            rows = self.conn.execute("""
                SELECT node_id, calibrated_at, n_subcarriers, cal_frames, metadata
                FROM calibrations ORDER BY node_id
            """).fetchall()

        return [
            {
                "node_id": r[0],
                "calibrated_at": r[1],
                "age_hours": round((time.time() - r[1]) / 3600.0, 1),
                "n_subcarriers": r[2],
                "cal_frames": r[3],
                "metadata": json.loads(r[4]) if r[4] else None,
            }
            for r in rows
        ]

    def close(self):
        self.conn.close()


# ── Frame Parser ─────────────────────────────────────────────────────────────

def parse_frame(data: bytes) -> Optional[CSIFrame]:
    """Parse a binary CSI frame from UDP. Returns None on invalid data."""
    if len(data) < HEADER_SIZE:
        return None

    magic, node_id, n_ant, n_sub, seq, rssi, noise, _reserved = struct.unpack_from(
        "<I B B H I b b H", data, 0
    )

    if magic != FRAME_MAGIC:
        return None

    expected_payload = 2 * n_ant * n_sub
    if len(data) < HEADER_SIZE + expected_payload:
        log.warning("Frame too short: node=%d, got %d bytes, expected %d",
                    node_id, len(data), HEADER_SIZE + expected_payload)
        return None

    # Extract amplitude and phase arrays
    amp_offset = HEADER_SIZE
    phase_offset = HEADER_SIZE + (n_ant * n_sub)

    amp_bytes = np.frombuffer(data, dtype=np.uint8,
                              count=n_ant * n_sub, offset=amp_offset)
    phase_bytes = np.frombuffer(data, dtype=np.int8,
                                count=n_ant * n_sub, offset=phase_offset)

    amplitudes = amp_bytes.reshape(n_ant, n_sub).astype(np.float32)
    phases = phase_bytes.reshape(n_ant, n_sub).astype(np.float32)

    # Un-scale: amplitudes were *4 on ESP32, phases were *40
    amplitudes /= 4.0
    phases /= 40.0   # back to approximate radians

    return CSIFrame(
        node_id=node_id,
        n_antennas=n_ant,
        n_subcarriers=n_sub,
        seq=seq,
        rssi=rssi,
        noise_floor=noise,
        amplitudes=amplitudes,
        phases=phases,
    )


# ── Calibration System ───────────────────────────────────────────────────────

def start_calibration(state: NodeState, duration: float = CALIBRATION_DURATION_SEC):
    """Begin baseline capture for a node. Call once on first frame or on-demand."""
    cal = state.calibration
    cal.calibrating = True
    cal.calibrated = False
    cal.cal_start_time = time.time()
    cal.cal_duration = duration
    cal.cal_frames = 0
    cal.cal_amp_buffer = []
    log.info("Node %d: calibration started (%.0fs window)", state.node_id, duration)


def feed_calibration(state: NodeState, amp_vector: np.ndarray) -> bool:
    """
    Feed one amplitude vector into the calibration buffer.
    Returns True when calibration completes.
    """
    cal = state.calibration
    if not cal.calibrating:
        return False

    cal.cal_amp_buffer.append(amp_vector.copy())
    cal.cal_frames += 1

    elapsed = time.time() - cal.cal_start_time
    if elapsed >= cal.cal_duration and cal.cal_frames >= CALIBRATION_MIN_FRAMES:
        _finalize_calibration(state)
        return True

    return False


def _finalize_calibration(state: NodeState):
    """Compute baseline mean, std, and 3-sigma thresholds from captured data."""
    cal = state.calibration
    amp_matrix = np.array(cal.cal_amp_buffer, dtype=np.float32)  # (N, n_sub)

    cal.baseline_mean = np.mean(amp_matrix, axis=0)
    cal.baseline_std = np.std(amp_matrix, axis=0)

    # Clamp std floor to avoid zero-threshold subcarriers (noise floor)
    std_floor = 0.5
    cal.baseline_std = np.maximum(cal.baseline_std, std_floor)

    # Per-subcarrier thresholds
    cal.threshold_presence = cal.baseline_mean + SIGMA_MULTIPLIER * cal.baseline_std
    cal.threshold_motion = cal.baseline_mean + MOTION_SIGMA_MULTIPLIER * cal.baseline_std

    # Initialize drift baseline as copy of calibration baseline
    cal.drift_baseline = cal.baseline_mean.copy()
    cal.drift_std = cal.baseline_std.copy()

    # Initialize EMA with baseline mean
    cal.ema_amplitude = cal.baseline_mean.copy()

    cal.calibrating = False
    cal.calibrated = True

    # Free buffer memory
    cal.cal_amp_buffer = []

    log.info(
        "Node %d: calibration COMPLETE — %d frames, "
        "mean_amp=%.2f, mean_std=%.3f, 3σ_threshold=%.2f",
        state.node_id, cal.cal_frames,
        float(np.mean(cal.baseline_mean)),
        float(np.mean(cal.baseline_std)),
        float(np.mean(cal.threshold_presence)),
    )


def ema_filter(current_ema: np.ndarray, new_sample: np.ndarray,
               alpha: float = EMA_ALPHA) -> np.ndarray:
    """Exponential moving average: smooths per-subcarrier amplitude."""
    return alpha * new_sample + (1.0 - alpha) * current_ema


def update_drift_baseline(state: NodeState, amp_vector: np.ndarray):
    """
    Slow-drift baseline correction. Only updates when the room appears empty
    (current deviation from baseline is well below presence threshold).
    This prevents the baseline from chasing real presence events.
    """
    cal = state.calibration
    if not cal.calibrated or cal.drift_baseline is None:
        return

    # Guard: only drift-update when deviation is small (room empty)
    deviation = np.abs(amp_vector - cal.drift_baseline)
    threshold_margin = BASELINE_DRIFT_GUARD * (cal.threshold_presence - cal.drift_baseline)
    threshold_margin = np.maximum(threshold_margin, 0.1)  # safety floor

    if np.all(deviation < threshold_margin):
        # Slow EMA update of the drift baseline
        cal.drift_baseline = (
            BASELINE_DRIFT_ALPHA * amp_vector
            + (1.0 - BASELINE_DRIFT_ALPHA) * cal.drift_baseline
        )
        # Update std estimate very slowly too
        residual = np.abs(amp_vector - cal.drift_baseline)
        cal.drift_std = (
            BASELINE_DRIFT_ALPHA * residual
            + (1.0 - BASELINE_DRIFT_ALPHA) * cal.drift_std
        )
        cal.drift_std = np.maximum(cal.drift_std, 0.5)  # floor

        # Recompute thresholds from drifted baseline
        cal.threshold_presence = cal.drift_baseline + SIGMA_MULTIPLIER * cal.drift_std
        cal.threshold_motion = cal.drift_baseline + MOTION_SIGMA_MULTIPLIER * cal.drift_std


# ── Signal Processing ────────────────────────────────────────────────────────

def compute_presence(state: NodeState) -> tuple[bool, bool, float]:
    """
    Phase B: Presence detection.
    Uses calibrated per-subcarrier 3-sigma thresholds when available,
    falls back to legacy variance method during calibration.
    Returns (present, motion, variance).
    """
    cal = state.calibration

    # ── Calibrated path: per-subcarrier threshold comparison ──
    if cal.calibrated and cal.ema_amplitude is not None:
        ema = cal.ema_amplitude
        n_sub = len(ema)

        # Count how many subcarriers exceed their per-channel threshold
        presence_exceeds = ema > cal.threshold_presence
        motion_exceeds = ema > cal.threshold_motion
        presence_frac = float(np.sum(presence_exceeds)) / n_sub
        motion_frac = float(np.sum(motion_exceeds)) / n_sub

        # Require at least 15% of subcarriers to exceed threshold
        presence_now = presence_frac > 0.15
        motion = motion_frac > 0.10

        # Also compute a variance-like metric for logging/publishing
        deviation = np.mean(np.abs(ema - cal.drift_baseline))
        mean_variance = float(deviation)

        if presence_now:
            state.last_presence_time = time.time()
            present = True
        else:
            elapsed = time.time() - state.last_presence_time
            present = elapsed < PRESENCE_HOLD_TIME and state.last_presence_time > 0

        return present, motion, mean_variance

    # ── Legacy fallback: global variance (pre-calibration) ──
    if len(state.amp_history) < 10:
        return False, False, 0.0

    amp_matrix = np.array(state.amp_history)
    variance_per_sub = np.var(amp_matrix, axis=0)
    mean_variance = float(np.mean(variance_per_sub))

    motion = mean_variance > MOTION_VARIANCE_THRESHOLD
    presence_now = mean_variance > PRESENCE_VARIANCE_THRESHOLD

    if presence_now:
        state.last_presence_time = time.time()
        present = True
    else:
        elapsed = time.time() - state.last_presence_time
        present = elapsed < PRESENCE_HOLD_TIME and state.last_presence_time > 0

    return present, motion, mean_variance


def compute_breathing(state: NodeState) -> tuple[Optional[float], float]:
    """
    Phase C: Breathing rate extraction via FFT on mean amplitude time series.
    Returns (bpm or None, confidence).
    """
    n = len(state.breathing_history)
    if n < 200:  # need at least ~10 seconds at 20 Hz
        return None, 0.0

    signal_arr = np.array(state.breathing_history, dtype=np.float64)
    timestamps = np.array(state.breathing_timestamps, dtype=np.float64)

    # Estimate sample rate from actual timestamps
    dt = np.median(np.diff(timestamps))
    if dt <= 0:
        return None, 0.0
    fs = 1.0 / dt

    # Detrend: remove mean
    signal_arr = signal_arr - np.mean(signal_arr)

    # Apply Hanning window
    window = np.hanning(n)
    signal_arr = signal_arr * window

    # FFT
    fft_vals = np.fft.rfft(signal_arr)
    fft_mag = np.abs(fft_vals)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)

    # Bandpass: only look at breathing frequencies
    mask = (freqs >= BREATHING_MIN_HZ) & (freqs <= BREATHING_MAX_HZ)
    if not np.any(mask):
        return None, 0.0

    band_mag = fft_mag[mask]
    band_freqs = freqs[mask]

    # Find dominant frequency in breathing band
    peak_idx = np.argmax(band_mag)
    peak_freq = band_freqs[peak_idx]
    peak_power = band_mag[peak_idx]

    # Confidence: ratio of peak power to total power in band
    total_power = np.sum(band_mag)
    confidence = float(peak_power / total_power) if total_power > 0 else 0.0

    if confidence < BREATHING_MIN_CONFIDENCE:
        return None, confidence

    bpm = peak_freq * 60.0
    return round(bpm, 1), round(confidence, 3)


# ── CSI Bridge ───────────────────────────────────────────────────────────────

class CSIBridge:
    """Main bridge: UDP listener + MQTT publisher."""

    def __init__(self, udp_port: int, mqtt_host: str, mqtt_port: int,
                 calibration_duration: float = CALIBRATION_DURATION_SEC,
                 db_path: str = DEFAULT_DB_PATH):
        self.udp_port = udp_port
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.calibration_duration = calibration_duration

        self.nodes: dict[int, NodeState] = {}
        self.running = False
        self.udp_sock: Optional[socket.socket] = None

        # SQLite calibration persistence
        self.cal_store = CalibrationStore(db_path)
        stored = self.cal_store.list_nodes()
        if stored:
            log.info("CalibrationStore: %d stored baselines: %s",
                     len(stored), [s["node_id"] for s in stored])

        # MQTT client — use CallbackAPIVersion for paho-mqtt v2 compat
        try:
            self.mqttc = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id="csi-bridge",
                protocol=mqtt.MQTTv311,
            )
        except (AttributeError, TypeError):
            # Fallback for paho-mqtt v1.x
            self.mqttc = mqtt.Client(
                client_id="csi-bridge",
                protocol=mqtt.MQTTv311,
            )
        self.mqttc.on_connect = self._on_mqtt_connect
        self.mqttc.on_message = self._on_mqtt_message

        # Will message: bridge offline
        self.mqttc.will_set(
            "home/csi/bridge/status", payload="offline", qos=1, retain=True
        )

    # ── MQTT callbacks ───────────────────────────────────────────────────

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected to %s:%d", self.mqtt_host, self.mqtt_port)
            client.publish("home/csi/bridge/status", "online", qos=1, retain=True)
            # Subscribe to zone assignment commands
            client.subscribe("home/csi/+/zone/set", qos=1)
            # Subscribe to recalibrate commands
            client.subscribe("home/csi/+/calibrate", qos=1)
        else:
            log.error("MQTT connection failed: rc=%d", rc)

    def _on_mqtt_message(self, client, userdata, msg):
        """Handle zone assignment and recalibrate commands."""
        parts = msg.topic.split("/")

        # Recalibrate command: home/csi/{node_id}/calibrate
        if len(parts) == 4 and parts[3] == "calibrate":
            try:
                node_id = int(parts[2])
            except ValueError:
                return
            if node_id in self.nodes:
                log.info("Node %d: recalibration requested via MQTT", node_id)
                start_calibration(self.nodes[node_id], self.calibration_duration)
                self._publish_calibration_status(self.nodes[node_id])
            return

        # Zone assignment: home/csi/{node_id}/zone/set
        if len(parts) == 5 and parts[3] == "zone" and parts[4] == "set":
            try:
                node_id = int(parts[2])
            except ValueError:
                log.warning("Invalid node_id in zone set topic: %s", msg.topic)
                return

            zone = msg.payload.decode("utf-8", errors="replace").strip()
            if not zone:
                log.warning("Empty zone for node %d", node_id)
                return

            if node_id in self.nodes:
                old_zone = self.nodes[node_id].zone
                self.nodes[node_id].zone = zone
                log.info("Node %d zone: %s → %s", node_id, old_zone, zone)
            else:
                log.info("Zone '%s' queued for node %d (not yet seen)", zone, node_id)
                # Pre-create state so zone is ready when node appears
                self.nodes[node_id] = NodeState(node_id=node_id, zone=zone)

            # Confirm via MQTT
            self.mqttc.publish(
                f"home/csi/{node_id}/zone",
                json.dumps({"node_id": node_id, "zone": zone}),
                qos=1, retain=True,
            )

    # ── UDP listener ─────────────────────────────────────────────────────

    def _udp_listener(self):
        """Receive and process UDP CSI frames."""
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_sock.settimeout(1.0)
        self.udp_sock.bind(("0.0.0.0", self.udp_port))
        log.info("UDP listening on 0.0.0.0:%d", self.udp_port)

        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(MAX_FRAME_SIZE)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    log.exception("UDP receive error")
                break

            frame = parse_frame(data)
            if frame is None:
                continue

            self._process_frame(frame, addr)

    # ── Frame processing ─────────────────────────────────────────────────

    def _process_frame(self, frame: CSIFrame, addr: tuple):
        """Process a parsed CSI frame: update state, calibrate, compute, publish."""
        nid = frame.node_id

        # Auto-discover new nodes
        if nid not in self.nodes:
            self.nodes[nid] = NodeState(node_id=nid)
            log.info("New node discovered: id=%d from %s", nid, addr[0])
            self.mqttc.publish(
                "home/csi/discovery",
                json.dumps({
                    "node_id": nid,
                    "ip": addr[0],
                    "subcarriers": frame.n_subcarriers,
                    "antennas": frame.n_antennas,
                    "timestamp": frame.timestamp,
                }),
                qos=1,
            )
            self.mqttc.publish(
                f"home/csi/{nid}/status", "online", qos=1, retain=True
            )
            # Try loading persisted calibration before starting fresh
            stored_cal = self.cal_store.load(nid)
            if stored_cal:
                self.nodes[nid].calibration = stored_cal
                log.info("Node %d: using persisted calibration (skipping 60s baseline)",
                         nid)
                self._publish_calibration_status(self.nodes[nid])
            else:
                start_calibration(self.nodes[nid], self.calibration_duration)

        state = self.nodes[nid]
        state.last_seen = frame.timestamp
        state.frame_count += 1
        state.last_seq = frame.seq

        # Store amplitude vector (antenna 0) for processing
        amp_vector = frame.amplitudes[0]  # shape: (n_subcarriers,)
        state.amp_history.append(amp_vector.copy())

        # Store scalar mean for breathing extraction
        scalar_mean = float(np.mean(amp_vector))
        state.breathing_history.append(scalar_mean)
        state.breathing_timestamps.append(frame.timestamp)

        now = time.time()
        cal = state.calibration

        # ── Calibration: feed frames into baseline capture ───────────────
        if cal.calibrating:
            just_finished = feed_calibration(state, amp_vector)
            if just_finished:
                self._publish_calibration_status(state)
                # Persist to SQLite
                self.cal_store.save(nid, state.calibration, metadata={
                    "rssi": frame.rssi if frame else None,
                    "noise_floor": frame.noise_floor if frame else None,
                    "n_subcarriers": frame.n_subcarriers if frame else None,
                })
            elif now - state.last_calibration_publish >= CALIBRATION_PUBLISH_INTERVAL:
                state.last_calibration_publish = now
                self._publish_calibration_status(state)
            # During calibration, still publish stats but skip presence/breathing
            if now - state.last_stats_publish >= STATS_INTERVAL:
                state.last_stats_publish = now
                self._publish_stats(state, frame)
            return

        # ── EMA filter on live readings ──────────────────────────────────
        if cal.calibrated and cal.ema_amplitude is not None:
            cal.ema_amplitude = ema_filter(cal.ema_amplitude, amp_vector)
            # Slow-drift baseline update (only when room appears empty)
            update_drift_baseline(state, cal.ema_amplitude)

        # ── Phase B: Presence detection ──────────────────────────────────
        present, motion, variance = compute_presence(state)
        state.present = present
        state.motion = motion
        state.last_variance = variance

        if now - state.last_presence_publish >= PRESENCE_INTERVAL:
            state.last_presence_publish = now
            self._publish_presence(state)

        # ── Phase C: Breathing extraction ────────────────────────────────
        if now - state.last_breathing_publish >= BREATHING_INTERVAL:
            state.last_breathing_publish = now
            bpm, confidence = compute_breathing(state)
            state.breathing_bpm = bpm
            state.breathing_confidence = confidence
            if bpm is not None and state.zone:
                self._publish_breathing(state)

        # ── CSI stats ────────────────────────────────────────────────────
        if now - state.last_stats_publish >= STATS_INTERVAL:
            state.last_stats_publish = now
            self._publish_stats(state, frame)

        # ── Periodic calibration status ──────────────────────────────────
        if cal.calibrated and now - state.last_calibration_publish >= 30.0:
            state.last_calibration_publish = now
            self._publish_calibration_status(state)

    # ── MQTT publishers ──────────────────────────────────────────────────

    def _publish_stats(self, state: NodeState, frame: CSIFrame):
        """Publish raw CSI stats for a node."""
        elapsed = state.last_seen - state.first_seen
        fps = state.frame_count / elapsed if elapsed > 0 else 0

        payload = {
            "node_id": state.node_id,
            "zone": state.zone,
            "rssi": frame.rssi,
            "noise_floor": frame.noise_floor,
            "subcarriers": frame.n_subcarriers,
            "antennas": frame.n_antennas,
            "seq": frame.seq,
            "frame_rate_hz": round(fps, 1),
            "frames_total": state.frame_count,
            "amplitude_mean": round(float(np.mean(frame.amplitudes)), 2),
            "amplitude_std": round(float(np.std(frame.amplitudes)), 2),
            "variance": round(state.last_variance, 2),
            "calibrated": state.calibration.calibrated,
            "calibrating": state.calibration.calibrating,
            "timestamp": round(state.last_seen, 3),
        }

        self.mqttc.publish(
            f"home/csi/{state.node_id}/stats",
            json.dumps(payload),
            qos=0,
        )

    def _publish_presence(self, state: NodeState):
        """Publish presence event. Uses zone topic if zone is assigned."""
        payload = {
            "present": state.present,
            "motion": state.motion,
            "variance": round(state.last_variance, 2),
            "source": "csi",
            "node_id": state.node_id,
            "timestamp": round(time.time(), 3),
        }

        # Always publish to node-level topic
        self.mqttc.publish(
            f"home/csi/{state.node_id}/presence",
            json.dumps(payload),
            qos=0,
        )

        # If zone assigned, also publish to the standard zone topic
        if state.zone:
            zone_payload = {
                "present": state.present,
                "motion": state.motion,
                "distance": None,  # CSI doesn't provide distance
                "source": "csi",
                "variance": round(state.last_variance, 2),
            }
            self.mqttc.publish(
                f"home/presence/{state.zone}",
                json.dumps(zone_payload),
                qos=0,
            )

    def _publish_breathing(self, state: NodeState):
        """Publish breathing rate for a zone."""
        if state.breathing_bpm is None or state.zone is None:
            return

        payload = {
            "breathing_bpm": state.breathing_bpm,
            "confidence": state.breathing_confidence,
            "source": "csi",
            "node_id": state.node_id,
            "timestamp": round(time.time(), 3),
        }

        self.mqttc.publish(
            f"home/breathing/{state.zone}",
            json.dumps(payload),
            qos=0,
        )

    def _publish_calibration_status(self, state: NodeState):
        """Publish calibration progress or baseline stats."""
        cal = state.calibration
        now = time.time()

        if cal.calibrating:
            elapsed = now - cal.cal_start_time
            progress = min(elapsed / cal.cal_duration, 1.0)
            payload = {
                "node_id": state.node_id,
                "status": "calibrating",
                "progress": round(progress, 2),
                "frames_collected": cal.cal_frames,
                "elapsed_sec": round(elapsed, 1),
                "duration_sec": cal.cal_duration,
                "timestamp": round(now, 3),
            }
        elif cal.calibrated:
            payload = {
                "node_id": state.node_id,
                "status": "calibrated",
                "baseline_mean": round(float(np.mean(cal.drift_baseline)), 3),
                "baseline_std": round(float(np.mean(cal.drift_std)), 3),
                "threshold_presence_mean": round(float(np.mean(cal.threshold_presence)), 3),
                "threshold_motion_mean": round(float(np.mean(cal.threshold_motion)), 3),
                "calibration_frames": cal.cal_frames,
                "timestamp": round(now, 3),
            }
        else:
            payload = {
                "node_id": state.node_id,
                "status": "uncalibrated",
                "timestamp": round(now, 3),
            }

        self.mqttc.publish(
            f"home/csi/{state.node_id}/calibration",
            json.dumps(payload),
            qos=0,
            retain=True,
        )

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        """Start the bridge: connect MQTT, start UDP listener."""
        self.running = True

        # Connect MQTT
        log.info("Connecting to MQTT broker at %s:%d", self.mqtt_host, self.mqtt_port)
        try:
            self.mqttc.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
        except Exception:
            log.exception("Failed to connect to MQTT broker")
            sys.exit(1)

        self.mqttc.loop_start()

        # Start UDP listener in main thread
        log.info("CSI Bridge starting — UDP :%d → MQTT %s:%d",
                 self.udp_port, self.mqtt_host, self.mqtt_port)
        try:
            self._udp_listener()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        """Graceful shutdown."""
        if not self.running:
            return
        self.running = False
        log.info("Shutting down CSI Bridge...")

        # Mark all nodes offline
        for nid in self.nodes:
            self.mqttc.publish(
                f"home/csi/{nid}/status", "offline", qos=1, retain=True
            )

        self.mqttc.publish("home/csi/bridge/status", "offline", qos=1, retain=True)
        self.mqttc.loop_stop()
        self.mqttc.disconnect()

        if self.udp_sock:
            self.udp_sock.close()

        self.cal_store.close()
        log.info("CSI Bridge stopped")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Project Presence — CSI Aggregator Bridge"
    )
    parser.add_argument(
        "--port", type=int, default=5005,
        help="UDP port to listen on (default: 5005)"
    )
    parser.add_argument(
        "--mqtt-host", type=str, default="127.0.0.1",
        help="MQTT broker hostname (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--mqtt-port", type=int, default=1883,
        help="MQTT broker port (default: 1883)"
    )
    parser.add_argument(
        "--calibration-duration", type=float, default=60.0,
        help="Baseline calibration window in seconds (default: 60)"
    )
    parser.add_argument(
        "--db-path", type=str, default=DEFAULT_DB_PATH,
        help=f"SQLite database path for calibration persistence (default: {DEFAULT_DB_PATH})"
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)"
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    bridge = CSIBridge(
        udp_port=args.port,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        calibration_duration=args.calibration_duration,
        db_path=args.db_path,
    )

    # Graceful shutdown on signals
    def handle_signal(signum, frame):
        log.info("Signal %d received, stopping...", signum)
        bridge.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    bridge.start()


if __name__ == "__main__":
    main()
