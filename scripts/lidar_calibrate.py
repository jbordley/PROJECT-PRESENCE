#!/usr/bin/env python3
"""
LiDAR Sector Calibration Tool
==============================
Interactive walk-around calibration for mapping LiDAR sectors to room landmarks.

Usage:
  python3 lidar_calibrate.py [--mqtt-host <broker-ip>] [--topic home/sentinel/node-01/lidar]

Modes:
  1. BASELINE  — Sit still, captures empty-room baseline for all 12 sectors
  2. IDENTIFY  — Prompts you to stand at each landmark, records which sectors change
  3. REVIEW    — Shows the full map and lets you label sectors
  4. EXPORT    — Writes room_config.json

The script does NOT require any firmware changes — it reads live MQTT data.
"""

import argparse
import json
import sys
import time
import statistics
from collections import defaultdict
from typing import Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("ERROR: pip install paho-mqtt")
    sys.exit(1)


# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 1883
DEFAULT_TOPIC = "home/sentinel/node-01/lidar"
NUM_SECTORS = 12
BASELINE_SAMPLES = 20        # Number of scans to average for baseline
IDENTIFY_SAMPLES = 10        # Number of scans per landmark
MIN_RANGE_MM = 120           # Same as firmware filter
CHANGE_THRESHOLD_PCT = 25    # % change from baseline to count as "blocked"


class LidarCalibrator:
    def __init__(self, host: str, port: int, topic: str):
        self.host = host
        self.port = port
        self.topic = topic
        self.samples: list[dict] = []
        self.collecting = False
        self.target_samples = 0

        self.baseline: dict[int, float] = {}          # sector → avg mm
        self.baseline_std: dict[int, float] = {}       # sector → std dev
        self.landmarks: dict[str, dict] = {}           # name → {sectors, distances}
        self.sector_labels: dict[int, str] = {}        # sector → label
        self.sector_types: dict[int, str] = {}         # sector → type

        # MQTT
        self.client = mqtt.Client(client_id="lidar-calibrator")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(self.topic)
        else:
            print(f"MQTT connect failed: rc={rc}")

    def _on_message(self, client, userdata, msg):
        if not self.collecting:
            return
        try:
            data = json.loads(msg.payload.decode())
            zones = {z["sector"]: z["min_mm"] for z in data.get("zones", [])
                     if z.get("min_mm", 0) >= MIN_RANGE_MM}
            self.samples.append(zones)
        except (json.JSONDecodeError, KeyError):
            pass

    def connect(self):
        print(f"Connecting to MQTT at {self.host}:{self.port}...")
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()
        time.sleep(1)
        print("Connected. Listening on:", self.topic)

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    def collect(self, num_samples: int, label: str = "") -> list[dict]:
        """Collect N lidar scans. Returns list of {sector: min_mm} dicts."""
        self.samples = []
        self.collecting = True
        self.target_samples = num_samples

        if label:
            print(f"  Collecting {num_samples} samples for '{label}'...")
        else:
            print(f"  Collecting {num_samples} samples...")

        while len(self.samples) < num_samples:
            time.sleep(0.5)
            remaining = num_samples - len(self.samples)
            print(f"\r  {len(self.samples)}/{num_samples}", end="", flush=True)

        self.collecting = False
        print(f"\r  {num_samples}/{num_samples} — done.")
        return list(self.samples)

    # ── Phase 1: Baseline ────────────────────────────────────────────────

    def capture_baseline(self):
        print("\n" + "=" * 60)
        print("PHASE 1: BASELINE CAPTURE")
        print("=" * 60)
        print("Clear the room of people (or sit perfectly still at your desk).")
        input("Press ENTER when ready...")

        samples = self.collect(BASELINE_SAMPLES, "baseline")

        # Compute per-sector average and std dev
        sector_readings: dict[int, list[float]] = defaultdict(list)
        for scan in samples:
            for sector in range(NUM_SECTORS):
                if sector in scan:
                    sector_readings[sector].append(scan[sector])

        print("\n  Baseline results:")
        print(f"  {'Sector':<8} {'Avg (mm)':<10} {'StdDev':<10} {'Samples':<8}")
        print("  " + "-" * 36)

        for s in range(NUM_SECTORS):
            readings = sector_readings.get(s, [])
            if readings:
                avg = statistics.mean(readings)
                std = statistics.stdev(readings) if len(readings) > 1 else 0
                self.baseline[s] = avg
                self.baseline_std[s] = std
                print(f"  S{s:<7} {avg:<10.0f} {std:<10.1f} {len(readings):<8}")
            else:
                self.baseline[s] = 0
                self.baseline_std[s] = 0
                print(f"  S{s:<7} {'(no data)':<10} {'—':<10} 0")

    # ── Phase 2: Landmark Identification ─────────────────────────────────

    def identify_landmarks(self):
        print("\n" + "=" * 60)
        print("PHASE 2: LANDMARK IDENTIFICATION")
        print("=" * 60)
        print("Walk to each landmark and stand still for a few seconds.")
        print("Type 'done' when finished adding landmarks.\n")

        predefined = [
            ("kitchen_entry", "Walk to the kitchen opening"),
            ("bathroom_area", "Walk toward the bathroom / front door"),
            ("desk_b", "Stand at Bob's desk"),
            ("desk_a", "Sit in Alice's chair"),
            ("front_door", "Walk to the front door area"),
        ]

        for name, instruction in predefined:
            response = input(f"  {instruction} — press ENTER when in position (or 'skip'): ").strip()
            if response.lower() == 'skip':
                continue
            if response.lower() == 'done':
                break

            samples = self.collect(IDENTIFY_SAMPLES, name)
            changed = self._find_changed_sectors(samples)

            if changed:
                self.landmarks[name] = changed
                sectors_str = ", ".join(f"S{s} ({d:.0f}mm, was {self.baseline.get(s, 0):.0f}mm)"
                                        for s, d in sorted(changed.items()))
                print(f"  → Detected in sectors: {sectors_str}\n")
            else:
                print(f"  → No significant change detected. Try standing closer.\n")

        # Custom landmarks
        while True:
            name = input("  Add custom landmark name (or 'done'): ").strip()
            if name.lower() == 'done' or not name:
                break
            input(f"  Walk to '{name}' — press ENTER when in position: ")
            samples = self.collect(IDENTIFY_SAMPLES, name)
            changed = self._find_changed_sectors(samples)
            if changed:
                self.landmarks[name] = changed
                sectors_str = ", ".join(f"S{s}" for s in sorted(changed))
                print(f"  → Detected in sectors: {sectors_str}\n")
            else:
                print(f"  → No significant change detected.\n")

    def _find_changed_sectors(self, samples: list[dict]) -> dict[int, float]:
        """Compare samples against baseline, return sectors with significant change."""
        sector_readings: dict[int, list[float]] = defaultdict(list)
        for scan in samples:
            for sector in range(NUM_SECTORS):
                if sector in scan:
                    sector_readings[sector].append(scan[sector])

        changed = {}
        for s in range(NUM_SECTORS):
            readings = sector_readings.get(s, [])
            baseline = self.baseline.get(s, 0)

            if not readings or baseline == 0:
                # Sector appeared when it wasn't in baseline
                if readings and not baseline:
                    changed[s] = statistics.mean(readings)
                continue

            avg = statistics.mean(readings)
            pct_change = abs(avg - baseline) / baseline * 100

            if pct_change >= CHANGE_THRESHOLD_PCT:
                changed[s] = avg

        return changed

    # ── Phase 3: Review & Label ──────────────────────────────────────────

    def review_and_label(self):
        print("\n" + "=" * 60)
        print("PHASE 3: REVIEW & LABEL SECTORS")
        print("=" * 60)

        # Auto-assign labels from landmark data
        sector_candidates: dict[int, list[str]] = defaultdict(list)
        for landmark, sectors in self.landmarks.items():
            for s in sectors:
                sector_candidates[s].append(landmark)

        print("\n  Auto-assigned labels (from walk-around):")
        for s in range(NUM_SECTORS):
            baseline = self.baseline.get(s, 0)
            candidates = sector_candidates.get(s, [])

            if candidates:
                label = candidates[0]  # Primary landmark
                self.sector_labels[s] = label
            elif baseline > 0 and baseline < 1000:
                label = "wall_close"
                self.sector_labels[s] = label
            elif baseline > 3000 or baseline == 0:
                label = "open_space"
                self.sector_labels[s] = label
            else:
                label = "unknown"
                self.sector_labels[s] = label

            # Infer type
            if any(kw in label for kw in ("chair", "desk", "seat")):
                self.sector_types[s] = "seat"
            elif any(kw in label for kw in ("door", "entry", "hall", "traffic")):
                self.sector_types[s] = "opening"
            elif any(kw in label for kw in ("wall", "monitor")):
                self.sector_types[s] = "wall"
            else:
                self.sector_types[s] = "traffic" if baseline == 0 or baseline > 2000 else "wall"

            status = f"S{s:<3} {baseline:>6.0f}mm  →  {label:<25} ({self.sector_types[s]})"
            print(f"  {status}")

        print("\n  You can manually edit room_config.json later to fine-tune labels and person assignments.")

    # ── Phase 4: Export ──────────────────────────────────────────────────

    def export_config(self, output_path: str):
        print("\n" + "=" * 60)
        print("PHASE 4: EXPORT")
        print("=" * 60)

        config = {
            "_doc": "LiDAR sector-to-room mapping. Generated by lidar_calibrate.py",
            "_version": "1.0",
            "_node": self.topic.split("/")[-2] if "/" in self.topic else "node-01",
            "_zone": "office",
            "_lidar": "YDLiDAR X4 Pro",
            "_calibrated": time.strftime("%Y-%m-%d %H:%M"),
            "sectors": {},
            "detection_rules": {
                "presence_threshold_pct": CHANGE_THRESHOLD_PCT,
                "presence_threshold_mm": 300,
                "traffic_sectors": [],
                "seat_sectors": {},
                "wall_sectors": [],
                "ignore_sectors": [],
            }
        }

        for s in range(NUM_SECTORS):
            key = f"S{s}"
            label = self.sector_labels.get(s, "unknown")
            baseline = self.baseline.get(s, 0)
            stype = self.sector_types.get(s, "unknown")

            config["sectors"][key] = {
                "angle_start": s * 30,
                "angle_end": (s + 1) * 30,
                "label": label,
                "baseline_mm": round(baseline) if baseline > 0 else None,
                "baseline_std_mm": round(self.baseline_std.get(s, 0), 1),
                "type": stype,
                "notes": ""
            }

            # Populate detection rules
            rules = config["detection_rules"]
            if stype == "traffic" or stype == "opening":
                rules["traffic_sectors"].append(key)
            elif stype == "seat":
                # Try to infer person from label
                if "desk_a" in label or "alice" in label:
                    rules["seat_sectors"][key] = "alice"
                elif "desk_b" in label or "bob" in label:
                    rules["seat_sectors"][key] = "bob"
                else:
                    rules["seat_sectors"][key] = label
            elif stype == "wall":
                rules["wall_sectors"].append(key)

        with open(output_path, 'w') as f:
            json.dump(config, f, indent=2)

        print(f"  Saved to: {output_path}")
        print("  Edit the file to add notes, fix labels, or adjust thresholds.")

    # ── Main Flow ────────────────────────────────────────────────────────

    def run(self, output_path: str):
        print("=" * 60)
        print("  LiDAR Sector Calibration Tool")
        print("  YDLiDAR X4 Pro — 12 sectors × 30°")
        print("=" * 60)

        self.connect()

        try:
            self.capture_baseline()
            self.identify_landmarks()
            self.review_and_label()
            self.export_config(output_path)
        except KeyboardInterrupt:
            print("\n\nCalibration interrupted.")
        finally:
            self.disconnect()

        print("\nCalibration complete!")


def main():
    parser = argparse.ArgumentParser(description="LiDAR Sector Calibration Tool")
    parser.add_argument("--mqtt-host", default=DEFAULT_HOST, help="MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, default=DEFAULT_PORT, help="MQTT broker port")
    parser.add_argument("--topic", default=DEFAULT_TOPIC, help="LiDAR MQTT topic")
    parser.add_argument("--output", default="sentinel/room_config.json", help="Output config path")
    args = parser.parse_args()

    cal = LidarCalibrator(args.mqtt_host, args.mqtt_port, args.topic)
    cal.run(args.output)


if __name__ == "__main__":
    main()
