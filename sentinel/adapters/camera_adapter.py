#!/usr/bin/env python3
"""
Camera-to-Sentinel Adapter
============================
Captures frames from Arducam OV5647 (CSI) and Topdon TC001 (USB thermal),
runs lightweight detection, and publishes SensorReading messages to MQTT.

Runs on <broker-host> (Pi 4, <pi-ip>) where cameras are physically connected.

Publishes to:
  sentinel/sensors/{zone}/camera/raw   — face/person detection from Arducam
  sentinel/sensors/{zone}/thermal/raw  — heat map + human blobs from TC001

Also serves latest snapshots via HTTP for dashboard embedding:
  http://{host}:8089/snapshot/camera.jpg
  http://{host}:8089/snapshot/thermal.jpg

Usage:
  python -m sentinel.adapters.camera_adapter [--mqtt-host <pi-ip>] [--zone office]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    from picamera2 import Picamera2
    HAS_PICAMERA2 = True
except ImportError:
    HAS_PICAMERA2 = False

import paho.mqtt.client as mqtt

from sentinel.config import SentinelConfig, CONFIG_PATH
from sentinel.topics import Sensors, System
from sentinel.schemas.messages import SensorReading, SensorHealth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sentinel.adapter.camera")


# ── Face Crop Collection ────────────────────────────────────────────────
# Save detected face crops for future identity enrollment (InsightFace etc.)
# Crops are stored in a rolling directory with a cap to avoid filling the SD card.
FACE_CROP_DIR = Path.home() / "Presence" / "data" / "face_crops"
FACE_CROP_MAX = 500        # max crops to keep (FIFO eviction)
FACE_CROP_MIN_SIZE = 40    # skip tiny detections below 40x40 px (likely false positives)
FACE_CROP_PADDING = 0.25   # expand bbox by 25% for better enrollment quality

# ── Detection Parameters ────────────────────────────────────────────────

# Haar cascade for face detection (ships with OpenCV)
FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"

# Thermal thresholds for human detection
HUMAN_TEMP_MIN_C = 30.0   # minimum skin surface temp at distance (tightened from 28)
HUMAN_TEMP_MAX_C = 40.0   # maximum plausible human temp (tightened from 42)
HUMAN_BLOB_MIN_PX = 200   # minimum pixel area for a "human-shaped" blob (raised from 50)
THERMAL_RESOLUTION = (256, 192)  # Topdon TC001 native resolution

# TC001 composite frame cropping:
# When USB output is a composite (e.g., 644x384), the thermal data occupies
# one half of the frame. Set to "left", "right", "top", "bottom", or "auto".
# "auto" crops the left half for wide composites, top half for tall composites.
THERMAL_CROP = "auto"


# ── Snapshot HTTP Server ────────────────────────────────────────────────

class SnapshotStore:
    """Thread-safe storage for latest camera snapshots."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshots: dict[str, bytes] = {}

    def put(self, name: str, jpeg_bytes: bytes):
        with self._lock:
            self._snapshots[name] = jpeg_bytes

    def get(self, name: str) -> Optional[bytes]:
        with self._lock:
            return self._snapshots.get(name)


_snapshot_store = SnapshotStore()


# ── Face Crop Helpers ──────────────────────────────────────────────────

def _ensure_crop_dir():
    """Create face crop directory if it doesn't exist."""
    FACE_CROP_DIR.mkdir(parents=True, exist_ok=True)


def _evict_old_crops():
    """Remove oldest crops when exceeding FACE_CROP_MAX (FIFO)."""
    crops = sorted(FACE_CROP_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    excess = len(crops) - FACE_CROP_MAX
    if excess > 0:
        for old in crops[:excess]:
            try:
                old.unlink()
            except OSError:
                pass


def _save_face_crop(frame_bgr, x: int, y: int, w: int, h: int, zone: str) -> Optional[str]:
    """Crop a face region with padding and save as JPEG. Returns path or None."""
    if w < FACE_CROP_MIN_SIZE or h < FACE_CROP_MIN_SIZE:
        return None

    fh, fw = frame_bgr.shape[:2]
    # Expand bbox by padding for better enrollment quality
    pad_x = int(w * FACE_CROP_PADDING)
    pad_y = int(h * FACE_CROP_PADDING)
    x0 = max(0, x - pad_x)
    y0 = max(0, y - pad_y)
    x1 = min(fw, x + w + pad_x)
    y1 = min(fh, y + h + pad_y)

    crop = frame_bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return None

    ts = time.strftime("%Y%m%d_%H%M%S")
    ms = int((time.time() % 1) * 1000)
    filename = f"{zone}_{ts}_{ms:03d}.jpg"
    path = FACE_CROP_DIR / filename

    try:
        _ensure_crop_dir()
        cv2.imwrite(str(path), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        _evict_old_crops()
        log.info("Face crop saved: %s (%dx%d)", filename, x1 - x0, y1 - y0)
        return str(path)
    except OSError as e:
        if "Read-only file system" in str(e):
            log.error("FACE CROP FAILED — Read-only file system at %s", FACE_CROP_DIR)
            log.error("  FIX: FACE_CROP_DIR must be under ~/Presence/ (not /var/lib/)")
            log.error("  FIX: Or add to ReadWritePaths in sentinel-camera-adapter.service")
            log.error("  Disabling face crop saving for this session to stop log spam.")
            # Monkey-patch to no-op so this error doesn't repeat every frame
            global _save_face_crop
            _save_face_crop = lambda *a, **kw: None
        else:
            log.exception("Failed to save face crop")
        return None
    except Exception:
        log.exception("Failed to save face crop")
        return None


class SnapshotHandler(BaseHTTPRequestHandler):
    """Serves latest camera/thermal snapshots as JPEG."""

    def do_GET(self):
        # Strip query string so cache-busting ?t=... doesn't break routing
        path = self.path.split("?", 1)[0]
        if path == "/snapshot/camera.jpg":
            self._serve("camera")
        elif path == "/snapshot/thermal.jpg":
            self._serve("thermal")
        elif path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        else:
            self.send_error(404)

    def _serve(self, name: str):
        data = _snapshot_store.get(name)
        if data:
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(503, f"No {name} snapshot available yet")

    def log_message(self, format, *args):
        pass  # suppress HTTP access logs


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def _start_snapshot_server(port: int):
    """Run snapshot HTTP server in a daemon thread."""
    server = ReusableHTTPServer(("0.0.0.0", port), SnapshotHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Snapshot server on http://0.0.0.0:%d", port)
    return server


# ── Arducam OV5647 Capture (CSI via picamera2) ─────────────────────────

class ArducamCapture:
    """Captures frames from Arducam OV5647 via picamera2/libcamera."""

    def __init__(self, resolution=(640, 480), zone: str = "office"):
        self.resolution = resolution
        self.zone = zone
        self._cam = None
        self._face_cascade = cv2.CascadeClassifier(FACE_CASCADE_PATH)
        if self._face_cascade.empty():
            log.warning("Face cascade not loaded — face detection disabled")

    def start(self):
        if not HAS_PICAMERA2:
            log.warning("picamera2 not available — Arducam capture disabled. "
                        "Install: pip install picamera2")
            return False

        try:
            self._cam = Picamera2()
            config = self._cam.create_still_configuration(
                main={"size": self.resolution, "format": "RGB888"}
            )
            self._cam.configure(config)
            self._cam.start()
            time.sleep(2)  # warm-up
            log.info("Arducam OV5647 started at %s", self.resolution)
            return True
        except Exception:
            log.exception("Failed to start Arducam")
            self._cam = None
            return False

    def capture_and_detect(self) -> Optional[dict]:
        """Capture frame, run face detection, return reading dict."""
        if not self._cam:
            return None

        try:
            frame = self._cam.capture_array()
        except Exception:
            log.exception("Arducam capture failed")
            return None

        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)

        # Encode snapshot as JPEG for HTTP serving (need BGR for crop saving too)
        # Convert RGB→BGR for OpenCV encoding
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

        # Face detection + crop collection for identity enrollment
        faces = []
        if not self._face_cascade.empty():
            detections = self._face_cascade.detectMultiScale(
                gray,
                scaleFactor=1.2,
                minNeighbors=5,
                minSize=(60, 60),
            )
            for (x, y, w, h) in detections:
                # Save face crop for future InsightFace enrollment
                crop_path = _save_face_crop(bgr, int(x), int(y), int(w), int(h), self.zone)
                faces.append({
                    "bbox": [int(x), int(y), int(w), int(h)],
                    "confidence": 0.7,  # Haar gives no confidence; placeholder
                    "face_id": None,    # InsightFace enrollment needed for ID
                    "crop_path": crop_path,  # path to saved crop for enrollment
                })

        # Person detection heuristic: faces imply persons
        persons_detected = len(faces)
        _, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 75])
        _snapshot_store.put("camera", jpeg.tobytes())

        return {
            "faces_detected": len(faces),
            "faces": faces,
            "persons_detected": persons_detected,
            "gait_signature": None,
            "frame_width": frame.shape[1],
            "frame_height": frame.shape[0],
        }

    def stop(self):
        if self._cam:
            try:
                self._cam.stop()
            except Exception:
                pass
            self._cam = None


# ── Topdon TC001 Capture (USB thermal via OpenCV) ──────────────────────

class ThermalCapture:
    """Captures thermal frames from Topdon TC001 via V4L2/OpenCV.

    The TC001 presents as a UVC device. In Y16 mode, pixel values are
    temperature in centi-Kelvin (e.g., 29515 = 295.15K = 22.0°C), giving
    real radiometric temperature data. Falls back to YUYV luminance
    mapping if Y16 is not available.
    """

    # Oddball resolutions the TC001 advertises that may contain raw Y16 data
    # packed as YUYV. 4*12305*2 = 98440 bytes ≈ 256*192*2 = 98304 bytes.
    Y16_CANDIDATE_SIZES = [
        (4, 12621),   # 100968 bytes — CONFIRMED working, 2664-byte header
        (4, 12305),   # 98440 bytes
        (8, 12578),   # 201248 bytes
    ]

    # TC001 raw frame format (confirmed via tc001_y16_probe.py):
    # - 2664-byte metadata header before thermal pixels
    # - Thermal data is 256x192 uint16 with 0x8000 (32768) bias
    # - Actual pixel range: 32768-33023 (0-255 above bias = 8-bit dynamic range)
    # - Two clusters visible: background (~32780-32806) and human (~32870-32940)
    # - NOT true centi-Kelvin, but biased uint16 with AGC-scaled contrast
    Y16_RAW_HEADER_BYTES = 2664
    Y16_RAW_BIAS = 32768

    # If this many consecutive frames are identical, assume the feed is frozen
    STALE_FRAME_LIMIT = 5
    # Minimum seconds between reconnect attempts
    RECONNECT_COOLDOWN_SEC = 10.0

    def __init__(self, device: int = -1, crop: str = "auto"):
        self._device = device
        self._cap = None
        self._crop = crop  # "left", "right", "top", "bottom", "auto", or "none"
        self._frame_w = 0
        self._frame_h = 0
        self._mode = "unknown"  # "y16", "y16_raw", "yuyv_lum", or "rgb"
        self._y16_raw_cap = None  # separate capture for oddball Y16 extraction
        # Staleness detection — catch frozen V4L2 buffers
        self._last_frame_hash: int = 0
        self._stale_count: int = 0
        self._last_reconnect: float = 0.0
        self._detected_dev: int = -1  # remember which /dev/videoN we opened

    def _find_thermal_device(self) -> int:
        """Auto-detect the TC001 by probing /dev/video* devices."""
        if self._device >= 0:
            log.info("Using explicit thermal device /dev/video%d", self._device)
            return self._device

        TC001_RESOLUTIONS = [(256, 192), (384, 288), (256, 384)]
        candidates = []

        # Pass 1: check default resolution of each device
        for i in range(10):
            try:
                cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
                if not cap.isOpened():
                    continue
                w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
                h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
                if (w, h) in TC001_RESOLUTIONS:
                    log.info("Found thermal device at /dev/video%d (%dx%d)", i, int(w), int(h))
                    cap.release()
                    return i
                # Remember openable devices for pass 2
                candidates.append(i)
                cap.release()
            except Exception:
                continue

        # Pass 2: try requesting 256x192 on each openable device
        # (TC001 often defaults to 640x480 but accepts 256x192)
        for i in candidates:
            try:
                cap = cv2.VideoCapture(i, cv2.CAP_V4L2)
                if not cap.isOpened():
                    continue
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 256)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 192)
                actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                cap.release()
                if (actual_w, actual_h) in TC001_RESOLUTIONS:
                    log.info("Found thermal device at /dev/video%d (accepted 256x192)", i)
                    return i
            except Exception:
                continue

        log.warning("No thermal camera auto-detected, trying /dev/video1")
        return 1  # video1 is more likely than video0 (CSI camera claims video0)

    def _try_y16_raw_extraction(self, dev: int) -> bool:
        """Try capturing an oddball resolution that contains TC001 raw thermal data.

        The TC001 advertises oddball resolutions (e.g. 4x12621) that contain a
        2664-byte metadata header followed by 256x192 uint16 thermal pixels.
        The pixels are biased by 0x8000 (32768) with ~8-bit dynamic range.
        Not true centi-Kelvin, but much higher fidelity than YUYV luminance.
        """
        needed = self.Y16_RAW_HEADER_BYTES + 256 * 192 * 2  # 2664 + 98304 = 100968

        for w, h in self.Y16_CANDIDATE_SIZES:
            try:
                cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
                if not cap.isOpened():
                    continue
                cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)

                actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if actual_w != w or actual_h != h:
                    cap.release()
                    continue

                # Grab a test frame
                for _ in range(3):
                    cap.grab()
                    cap.retrieve()
                if not cap.grab():
                    cap.release()
                    continue
                ret, frame = cap.retrieve()
                if not ret or frame is None:
                    cap.release()
                    continue

                raw_bytes = frame.flatten()
                if len(raw_bytes) < needed:
                    log.info("Y16 raw: %dx%d frame too small (%d bytes, need %d)",
                             w, h, len(raw_bytes), needed)
                    cap.release()
                    continue

                # Skip header, extract thermal pixels
                thermal_bytes = raw_bytes[self.Y16_RAW_HEADER_BYTES:
                                          self.Y16_RAW_HEADER_BYTES + 256 * 192 * 2]
                raw16 = np.frombuffer(thermal_bytes.tobytes(), dtype=np.uint16)
                raw16 = raw16.reshape((192, 256))

                median_val = float(np.median(raw16))
                min_val = float(np.min(raw16))
                max_val = float(np.max(raw16))
                val_range = max_val - min_val

                log.info("Y16 raw probe %dx%d (header=%d): median=%.0f min=%.0f max=%.0f range=%.0f",
                         w, h, self.Y16_RAW_HEADER_BYTES, median_val, min_val, max_val, val_range)

                # Validation: values should be biased around 0x8000 (32768) with
                # reasonable dynamic range (50-500 counts for a typical indoor scene)
                if (min_val >= self.Y16_RAW_BIAS and
                        max_val < self.Y16_RAW_BIAS + 1024 and
                        val_range > 10):
                    log.info("Y16 RAW MODE CONFIRMED via %dx%d — "
                             "bias=%d, range=%d counts, %d-byte header skipped",
                             w, h, self.Y16_RAW_BIAS, int(val_range),
                             self.Y16_RAW_HEADER_BYTES)
                    self._y16_raw_cap = cap
                    self._y16_raw_size = (w, h)
                    return True
                else:
                    log.info("Y16 raw: values don't match expected pattern, skipping %dx%d", w, h)
                    cap.release()

            except Exception as e:
                log.debug("Y16 raw probe failed for %dx%d: %s", w, h, e)
                try:
                    cap.release()
                except Exception:
                    pass
                continue

        return False

    def _log_diagnostic(self, dev: int, reason: str):
        """Log a detailed diagnostic when thermal startup fails.

        This exists so we never have to debug these issues from scratch again.
        Each known failure mode gets a specific error message with the exact fix.
        """
        log.error("=" * 70)
        log.error("THERMAL CAMERA STARTUP FAILED — /dev/video%d — %s", dev, reason)
        log.error("-" * 70)

        if reason == "cannot_open":
            log.error("DIAGNOSIS: /dev/video%d could not be opened by OpenCV.", dev)
            log.error("  Common causes:")
            log.error("  1) THERMAL_DEVICE=%d in .env is WRONG (CSI cam = video0, TC001 = video1)", dev)
            log.error("     FIX: sed -i 's/THERMAL_DEVICE=%d/THERMAL_DEVICE=1/' ~/Presence/.env", dev)
            log.error("  2) TC001 is unplugged or USB port lost power (Pi undervoltage)")
            log.error("     FIX: Check USB cable, run 'lsusb | grep 2bdf' — should show TC001")
            log.error("     FIX: If missing, replug TC001 then: sudo systemctl restart sentinel-camera-adapter")
            log.error("  3) Another process has /dev/video%d locked", dev)
            log.error("     FIX: sudo fuser /dev/video%d", dev)
        elif reason == "y16_and_yuyv_both_failed":
            log.error("DIAGNOSIS: Device opened but neither Y16 raw nor YUYV capture worked.")
            log.error("  FIX: Power-cycle the TC001 (unplug USB, wait 5s, replug)")
            log.error("  FIX: Then: sudo systemctl restart sentinel-camera-adapter")
        elif reason == "read_only_fs":
            log.error("DIAGNOSIS: ProtectSystem=strict blocks writes outside ReadWritePaths.")
            log.error("  FIX: Ensure FACE_CROP_DIR is under ~/Presence/ (not /var/lib/)")
            log.error("  FIX: Or add path to ReadWritePaths in sentinel-camera-adapter.service")
        elif reason == "frozen_frames":
            log.error("DIAGNOSIS: V4L2 buffer queue stuck — grab() returns stale frames.")
            log.error("  FIX: Automatic reconnect should handle this. If persistent:")
            log.error("  FIX: Power-cycle TC001 USB, then: sudo systemctl restart sentinel-camera-adapter")

        log.error("  CURRENT ENV: grep THERMAL_DEVICE ~/Presence/.env")
        log.error("  USB CHECK:   lsusb | grep 2bdf")
        log.error("  V4L2 CHECK:  v4l2-ctl --list-devices")
        log.error("=" * 70)

    def start(self) -> bool:
        dev = self._find_thermal_device()
        self._detected_dev = dev

        try:
            # ── Path B: Try oddball resolutions for hidden Y16 data ──
            # Do this FIRST with a separate capture handle, so it doesn't
            # interfere with the main YUYV capture if it fails.
            if self._try_y16_raw_extraction(dev):
                self._mode = "y16_raw"
                self._frame_w = 256
                self._frame_h = 192
                log.info("TC001 using Y16 raw extraction mode via %dx%d",
                         self._y16_raw_size[0], self._y16_raw_size[1])
                # Don't need a separate YUYV capture
                self._cap = self._y16_raw_cap
                return True

            # ── Path A: Native 256x192 YUYV (no composite crop) ──────
            self._cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
            if not self._cap.isOpened():
                self._log_diagnostic(dev, "cannot_open")
                self._cap = None
                return False

            self._cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)

            # Request native 256x192 — pure thermal, no composite, no crop needed
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 256)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 192)

            self._frame_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self._frame_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

            if self._frame_w == 256 and self._frame_h == 192:
                log.info("TC001 at /dev/video%d — native 256x192 YUYV (no crop needed)",
                         dev)
                self._crop = "none"  # override crop since we got native res
            else:
                log.info("TC001 at /dev/video%d — %dx%d (crop=%s)",
                         dev, self._frame_w, self._frame_h, self._crop)

            self._mode = "auto_detect"  # will resolve on first frame

            # Warm-up: discard first few frames (TC001 often sends garbage initially)
            # Use grab()+retrieve() to match capture loop (no tearing)
            for _ in range(5):
                self._cap.grab()
                self._cap.retrieve()

            # Detect if this is a composite frame needing crop
            crop_mode = self._resolve_crop_mode()
            if crop_mode != "none":
                log.info("Composite frame detected — crop mode: %s", crop_mode)
            else:
                log.info("Native resolution — no cropping needed")

            return True
        except Exception:
            log.exception("Failed to open thermal camera")
            self._cap = None
            return False

    def _resolve_crop_mode(self) -> str:
        """Determine crop mode based on frame dimensions."""
        w, h = self._frame_w, self._frame_h
        crop = self._crop

        # Native resolution — no crop needed
        if (w, h) == (256, 192) or (w, h) == (384, 288):
            return "none"

        if crop == "none":
            return "none"

        if crop == "auto":
            # Wide composite (e.g., 644x384): thermal is usually in the left half
            if w > h * 1.5:
                return "left"
            # Tall composite (e.g., 256x384): thermal is usually the top half
            elif h > w * 1.5:
                return "top"
            # Square-ish but larger than native: try left half
            elif w > 400:
                return "left"
            return "none"

        return crop

    def _crop_frame(self, frame: np.ndarray) -> np.ndarray:
        """Crop composite frame to extract thermal-only portion."""
        mode = self._resolve_crop_mode()
        h, w = frame.shape[:2]

        if mode == "left":
            return frame[:, :w // 2]
        elif mode == "right":
            return frame[:, w // 2:]
        elif mode == "top":
            return frame[:h // 2, :]
        elif mode == "bottom":
            return frame[h // 2:, :]
        return frame

    def _reconnect(self) -> bool:
        """Release and re-open the thermal capture to recover from frozen buffers."""
        now = time.time()
        if now - self._last_reconnect < self.RECONNECT_COOLDOWN_SEC:
            return self._cap is not None and self._cap.isOpened()
        self._last_reconnect = now

        log.warning("RECONNECT: releasing thermal capture and re-opening /dev/video%d",
                     self._detected_dev)
        try:
            if self._cap:
                self._cap.release()
                self._cap = None
        except Exception:
            pass

        saved_mode = self._mode
        dev = self._detected_dev

        try:
            if saved_mode == "y16_raw":
                # Re-try oddball Y16 extraction
                self._y16_raw_cap = None
                if self._try_y16_raw_extraction(dev):
                    self._cap = self._y16_raw_cap
                    self._mode = "y16_raw"
                    log.info("RECONNECT: Y16 raw mode restored")
                    # Warm-up
                    for _ in range(3):
                        self._cap.grab()
                        self._cap.retrieve()
                    self._stale_count = 0
                    self._last_frame_hash = 0
                    return True
                # Fall through to YUYV if Y16 raw fails on reconnect
                log.warning("RECONNECT: Y16 raw failed, trying YUYV")

            cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
            if not cap.isOpened():
                log.error("RECONNECT: cannot open /dev/video%d", dev)
                return False
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 256)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 192)
            self._cap = cap
            self._mode = "auto_detect"
            # Warm-up
            for _ in range(5):
                self._cap.grab()
                self._cap.retrieve()
            self._stale_count = 0
            self._last_frame_hash = 0
            log.info("RECONNECT: thermal capture restored (auto_detect mode)")
            return True
        except Exception:
            log.exception("RECONNECT: failed to re-open thermal capture")
            self._cap = None
            return False

    def capture_and_detect(self) -> Optional[dict]:
        """Capture thermal frame, extract temperature map, detect human blobs."""
        if not self._cap:
            # Try to reconnect if we lost the capture
            if not self._reconnect():
                return None

        # grab+retrieve avoids torn frames from read() mid-scanout
        if not self._cap.grab():
            log.warning("Thermal grab failed — attempting reconnect")
            if self._reconnect():
                if not self._cap.grab():
                    return None
            else:
                return None
        ret, frame = self._cap.retrieve()
        if not ret or frame is None:
            log.warning("Thermal retrieve failed")
            return None

        # ── Staleness detection ───────────────────────────────────
        # Hash a small sample of the frame to detect frozen buffers.
        # If N consecutive frames hash the same, the V4L2 buffer is stuck.
        sample = frame.flat[::997]  # sparse sample — fast, low-collision
        frame_hash = hash(sample.tobytes())
        if frame_hash == self._last_frame_hash:
            self._stale_count += 1
            if self._stale_count >= self.STALE_FRAME_LIMIT:
                self._log_diagnostic(self._detected_dev, "frozen_frames")
                log.warning("Thermal feed FROZEN (%d identical frames) — reconnecting",
                            self._stale_count)
                if self._reconnect():
                    self._stale_count = 0
                    # Re-grab after reconnect
                    if not self._cap.grab():
                        return None
                    ret, frame = self._cap.retrieve()
                    if not ret or frame is None:
                        return None
                    sample = frame.flat[::997]
                    frame_hash = hash(sample.tobytes())
                else:
                    return None
        else:
            self._stale_count = 0
        self._last_frame_hash = frame_hash

        # ── Y16 raw extraction mode ─────────────────────────────────
        # Oddball resolution frame: skip 2664-byte header, extract 256x192 uint16,
        # subtract 0x8000 bias, and map to calibrated temperature range.
        if self._mode == "y16_raw":
            raw_bytes = frame.flatten()
            start = self.Y16_RAW_HEADER_BYTES
            needed = start + 256 * 192 * 2  # 2664 + 98304
            if len(raw_bytes) < needed:
                log.warning("Y16 raw frame too small: %d bytes (need %d)", len(raw_bytes), needed)
                return None
            thermal_bytes = raw_bytes[start:start + 256 * 192 * 2]
            raw16 = np.frombuffer(thermal_bytes.tobytes(), dtype=np.uint16)
            raw16 = raw16.reshape((192, 256))

            # Remove 0x8000 bias → 0-255 range (AGC-scaled 8-bit-equivalent)
            # Map linearly to calibrated indoor temp range (same as YUYV path)
            debiased = (raw16.astype(np.float32) - self.Y16_RAW_BIAS)
            # Normalize to 0.0-1.0 using observed dynamic range
            dmax = float(debiased.max())
            if dmax > 0:
                normalized = debiased / dmax
            else:
                normalized = debiased
            temp_c = 15.0 + normalized * 30.0  # 15-45°C indoor range
            vis_frame = raw16
        else:
            # Crop composite frame to thermal-only portion
            frame = self._crop_frame(frame)

            # ── Temperature extraction ──────────────────────────────
            # Auto-detect mode from frame data on first call
            if self._mode == "auto_detect":
                if frame.dtype in (np.uint16, np.int16):
                    self._mode = "y16"
                elif len(frame.shape) == 3 and frame.shape[2] == 2:
                    # 2-channel uint8 — could be Y16 packed as 2×uint8, or YUYV
                    test16 = frame[:, :, 0].astype(np.uint16) | (frame[:, :, 1].astype(np.uint16) << 8)
                    if test16.max() > 1000:
                        self._mode = "y16"
                    else:
                        self._mode = "yuyv_lum"
                elif len(frame.shape) == 3 and frame.shape[2] == 3:
                    self._mode = "rgb"
                else:
                    self._mode = "yuyv_lum"
                log.info("TC001 auto-detected mode: %s (dtype=%s shape=%s)",
                         self._mode, frame.dtype, frame.shape)

            if self._mode == "y16":
                # 16-bit pixel values are centi-Kelvin
                if len(frame.shape) == 3 and frame.shape[2] == 2:
                    raw16 = frame[:, :, 0].astype(np.uint16) | (frame[:, :, 1].astype(np.uint16) << 8)
                else:
                    raw16 = frame.astype(np.uint16)
                    if len(raw16.shape) == 3:
                        raw16 = raw16[:, :, 0]

                temp_c = raw16.astype(np.float32) / 100.0 - 273.15
                vis_frame = raw16
            elif self._mode == "rgb":
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
                temp_c = 15.0 + (gray / 255.0) * 30.0
                vis_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            else:
                # yuyv_lum: extract Y channel from YUYV
                if len(frame.shape) == 3 and frame.shape[2] == 2:
                    frame = frame[:, :, 0]
                elif len(frame.shape) == 3 and frame.shape[2] == 3:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if len(frame.shape) == 3 and frame.shape[2] == 1:
                    frame = frame[:, :, 0]

                raw = frame.astype(np.float32)
                temp_c = 15.0 + (raw / 255.0) * 30.0
                vis_frame = frame

        # Statistics
        max_temp = float(np.max(temp_c))
        min_temp = float(np.min(temp_c))
        mean_temp = float(np.mean(temp_c))

        # Human blob detection: threshold for human skin temp range
        human_mask = ((temp_c >= HUMAN_TEMP_MIN_C) & (temp_c <= HUMAN_TEMP_MAX_C)).astype(np.uint8) * 255

        # Find contours of human-temperature regions
        if len(human_mask.shape) == 3:
            human_mask = human_mask[:, :, 0]

        contours, _ = cv2.findContours(human_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        hot_spots = []
        human_blobs = 0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < HUMAN_BLOB_MIN_PX:
                continue

            human_blobs += 1
            M = cv2.moments(cnt)
            cx = int(M["m10"] / M["m00"]) if M["m00"] > 0 else 0
            cy = int(M["m01"] / M["m00"]) if M["m00"] > 0 else 0

            # Get peak temperature within this contour
            mask_single = np.zeros_like(human_mask)
            cv2.drawContours(mask_single, [cnt], -1, 255, -1)
            blob_temps = temp_c[mask_single > 0]
            peak_temp = float(np.max(blob_temps)) if len(blob_temps) > 0 else max_temp

            hot_spots.append({
                "x": cx,
                "y": cy,
                "temp_c": round(peak_temp, 1),
                "area_px": int(area),
            })

        # Generate colorized thermal JPEG for dashboard
        # Use vis_frame (raw 16-bit or 8-bit) for full dynamic range normalization
        if len(vis_frame.shape) == 3:
            vis_2d = vis_frame[:, :, 0]
        else:
            vis_2d = vis_frame
        norm = cv2.normalize(vis_2d, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        colorized = cv2.applyColorMap(norm, cv2.COLORMAP_INFERNO)

        _, jpeg = cv2.imencode(".jpg", colorized, [cv2.IMWRITE_JPEG_QUALITY, 80])
        _snapshot_store.put("thermal", jpeg.tobytes())

        return {
            "max_temp_c": round(max_temp, 1),
            "min_temp_c": round(min_temp, 1),
            "mean_temp_c": round(mean_temp, 1),
            "hot_spots": hot_spots[:10],  # limit to top 10
            "human_shaped_blobs": human_blobs,
            "frame_width": temp_c.shape[1] if len(temp_c.shape) > 1 else 0,
            "frame_height": temp_c.shape[0],
            "mode": self._mode,  # "y16" = real temp, "yuyv_lum" = estimated
        }

    def stop(self):
        if self._cap:
            self._cap.release()
            self._cap = None
        if self._y16_raw_cap and self._y16_raw_cap is not self._cap:
            self._y16_raw_cap.release()
            self._y16_raw_cap = None


# ── Camera Adapter (orchestrator) ───────────────────────────────────────

class CameraAdapter:
    """Captures from both cameras on a timer and publishes SensorReadings."""

    def __init__(self, config: SentinelConfig, zone: str = "office",
                 camera_interval: float = 1.0, thermal_interval: float = 2.0,
                 thermal_device: int = -1, thermal_crop: str = "auto",
                 snapshot_port: int = 8089):
        self.config = config
        self.zone = zone
        self.camera_interval = camera_interval
        self.thermal_interval = thermal_interval
        self.running = False

        self._arducam = ArducamCapture(zone=zone)
        self._thermal = ThermalCapture(device=thermal_device, crop=thermal_crop)
        self._snapshot_port = snapshot_port

        # MQTT client
        client_id = f"{config.mqtt.client_id_prefix}-camera-adapter"
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

        if config.mqtt.username:
            self.mqttc.username_pw_set(config.mqtt.username, config.mqtt.password)

        # Stats
        self._camera_count = 0
        self._thermal_count = 0

    def _publish_camera(self):
        """Capture and publish one camera reading."""
        result = self._arducam.capture_and_detect()
        if result is None:
            return

        confidence = 0.85 if result["faces_detected"] > 0 else 0.15

        reading = SensorReading(
            node_id="<broker-host>-cam",
            zone=self.zone,
            sensor_type="camera",
            reading=result,
            confidence=round(confidence, 3),
            health=SensorHealth.NOMINAL.value,
            environment={},
            physics_plausible=True,
        )

        topic = Sensors.raw(self.zone, "camera")
        self.mqttc.publish(topic, reading.to_json(), qos=0)
        self._camera_count += 1

    def _publish_thermal(self):
        """Capture and publish one thermal reading."""
        result = self._thermal.capture_and_detect()
        if result is None:
            return

        blobs = result["human_shaped_blobs"]
        confidence = min(0.5 + blobs * 0.2, 0.9) if blobs > 0 else 0.1

        reading = SensorReading(
            node_id="<broker-host>-thermal",
            zone=self.zone,
            sensor_type="thermal",
            reading=result,
            confidence=round(confidence, 3),
            health=SensorHealth.NOMINAL.value,
            environment={},
            physics_plausible=True,
        )

        topic = Sensors.raw(self.zone, "thermal")
        self.mqttc.publish(topic, reading.to_json(), qos=0)
        self._thermal_count += 1

    def _camera_loop(self):
        """Camera capture loop running in its own thread."""
        while self.running:
            try:
                self._publish_camera()
            except Exception:
                log.exception("Camera capture error")
            time.sleep(self.camera_interval)

    def _thermal_loop(self):
        """Thermal capture loop running in its own thread."""
        while self.running:
            try:
                self._publish_thermal()
            except Exception:
                log.exception("Thermal capture error")
            time.sleep(self.thermal_interval)

    def start(self):
        self.running = True
        cfg = self.config.mqtt

        # Connect MQTT
        log.info("Connecting to MQTT at %s:%d", cfg.host, cfg.port)
        try:
            self.mqttc.connect(cfg.host, cfg.port, keepalive=cfg.keepalive)
        except Exception:
            log.exception("Failed to connect to MQTT")
            sys.exit(1)
        self.mqttc.loop_start()

        # Start snapshot HTTP server
        _start_snapshot_server(self._snapshot_port)

        # Start camera captures
        cam_ok = self._arducam.start()
        therm_ok = self._thermal.start()

        if not cam_ok and not therm_ok:
            log.error("Neither camera could be opened — exiting")
            sys.exit(1)

        threads = []
        if cam_ok:
            t = threading.Thread(target=self._camera_loop, name="arducam", daemon=True)
            t.start()
            threads.append(t)
            log.info("Arducam publishing to %s every %.1fs",
                     Sensors.raw(self.zone, "camera"), self.camera_interval)

        if therm_ok:
            t = threading.Thread(target=self._thermal_loop, name="thermal", daemon=True)
            t.start()
            threads.append(t)
            log.info("TC001 publishing to %s every %.1fs",
                     Sensors.raw(self.zone, "thermal"), self.thermal_interval)

        log.info("Camera adapter started (zone=%s)", self.zone)

        try:
            while self.running:
                time.sleep(10)
                log.debug("Camera: %d frames, Thermal: %d frames",
                          self._camera_count, self._thermal_count)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        if not self.running:
            return
        self.running = False
        log.info("Camera adapter stopping...")
        log.info("Final stats: camera=%d, thermal=%d",
                 self._camera_count, self._thermal_count)
        self._arducam.stop()
        self._thermal.stop()
        self.mqttc.loop_stop()
        self.mqttc.disconnect()
        log.info("Camera adapter stopped")


# ── CLI ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SENTINEL Camera Adapter")
    parser.add_argument("--config", type=str, default=CONFIG_PATH)
    parser.add_argument("--mqtt-host", type=str, default=None)
    parser.add_argument("--mqtt-port", type=int, default=None)
    parser.add_argument("--zone", type=str, default="office",
                        help="Zone where cameras are located")
    parser.add_argument("--camera-interval", type=float, default=1.0,
                        help="Seconds between Arducam captures (default: 1.0)")
    parser.add_argument("--thermal-interval", type=float, default=2.0,
                        help="Seconds between thermal captures (default: 2.0)")
    parser.add_argument("--thermal-device", type=int, default=-1,
                        help="/dev/video index for TC001 (-1 = auto-detect)")
    parser.add_argument("--thermal-crop", type=str, default="auto",
                        choices=["auto", "left", "right", "top", "bottom", "none"],
                        help="Crop mode for TC001 composite frames (default: auto)")
    parser.add_argument("--snapshot-port", type=int, default=8089,
                        help="HTTP port for snapshot serving (default: 8089)")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    config = SentinelConfig.load(args.config)
    if args.mqtt_host:
        config.mqtt.host = args.mqtt_host
    if args.mqtt_port:
        config.mqtt.port = args.mqtt_port

    adapter = CameraAdapter(
        config,
        zone=args.zone,
        camera_interval=args.camera_interval,
        thermal_interval=args.thermal_interval,
        thermal_device=args.thermal_device,
        thermal_crop=args.thermal_crop,
        snapshot_port=args.snapshot_port,
    )

    def handle_signal(signum, frame):
        adapter.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    adapter.start()


if __name__ == "__main__":
    main()
