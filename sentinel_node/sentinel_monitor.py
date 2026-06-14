#!/usr/bin/env python3
"""
Sentinel Monitor — Real-time sensor data viewer
Listens on UDP (radar) + MQTT (all topics) and prints to terminal.
Run on the hub (Raspberry Pi) or any machine on the same network.

Usage:
    pip install paho-mqtt
    python sentinel_monitor.py [--broker <broker-ip>] [--udp-port 5005]
"""

import argparse
import json
import socket
import struct
import threading
import time
import sys
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Install paho-mqtt:  pip install paho-mqtt")
    sys.exit(1)

# --- ANSI colors for terminal output ---
C_RESET  = "\033[0m"
C_RADAR  = "\033[96m"    # Cyan
C_DEVICE = "\033[93m"    # Yellow
C_ENV    = "\033[92m"    # Green
C_AUDIO  = "\033[95m"    # Magenta
C_STATUS = "\033[90m"    # Gray
C_CMD    = "\033[91m"    # Red
C_BOLD   = "\033[1m"

TOPIC_COLORS = {
    "radar":       C_RADAR,
    "devices":     C_DEVICE,
    "environment": C_ENV,
    "acoustic":    C_AUDIO,
    "status":      C_STATUS,
}

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

# ============================================================
# UDP Listener — Raw radar frames (10Hz binary)
# ============================================================
def udp_listener(port, show_raw=False):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    print(f"{C_BOLD}[UDP] Listening on port {port}{C_RESET}")

    while True:
        data, addr = sock.recvfrom(1024)
        if len(data) < 6:
            continue

        # Parse: [id_len:1][node_id:N][target_count:1][ts:4][targets...]
        id_len = data[0]
        node_id = data[1:1+id_len].decode("utf-8", errors="replace")
        payload = data[1+id_len:]

        if len(payload) < 5:
            continue

        target_count = payload[0]
        timestamp = struct.unpack_from("<I", payload, 1)[0]

        targets = []
        offset = 5
        for i in range(target_count):
            if offset + 8 > len(payload):
                break
            x, y, spd, dist = struct.unpack_from("<hhhH", payload, offset)
            targets.append({"x": x, "y": y, "spd": spd, "dist": dist})
            offset += 8

        # Format output
        tgt_str = "  ".join(
            f"T{i+1}({t['x']:+5d},{t['y']:+5d}) spd={t['spd']:+4d} d={t['dist']:4d}"
            for i, t in enumerate(targets)
        ) if targets else "no targets"

        print(f"{C_RADAR}[{ts()}] [{node_id}] RADAR  n={target_count}  {tgt_str}{C_RESET}")

# ============================================================
# MQTT Subscriber — All sentinel topics
# ============================================================
def mqtt_listener(broker, port):
    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"{C_BOLD}[MQTT] Connected to {broker}:{port}{C_RESET}")
            client.subscribe("home/sentinel/#")
        else:
            print(f"{C_CMD}[MQTT] Connect failed rc={rc}{C_RESET}")

    def on_message(client, userdata, msg):
        topic = msg.topic
        # Extract subtopic (last segment) and node_id
        parts = topic.split("/")
        subtopic = parts[-1] if len(parts) > 0 else topic
        node_id = parts[2] if len(parts) > 2 else "?"

        color = TOPIC_COLORS.get(subtopic, C_CMD)

        try:
            payload = json.loads(msg.payload.decode())
            formatted = format_payload(subtopic, payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            formatted = msg.payload.decode("utf-8", errors="replace")

        print(f"{color}[{ts()}] [{node_id}] {subtopic.upper():12s} {formatted}{C_RESET}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sentinel-monitor")
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(broker, port, 60)
    except Exception as e:
        print(f"{C_CMD}[MQTT] Cannot connect to {broker}:{port} — {e}{C_RESET}")
        print(f"{C_CMD}[MQTT] Will retry in background...{C_RESET}")

    client.loop_start()


def format_payload(subtopic, data):
    """Pretty-print known payload types."""
    if subtopic == "radar":
        targets = data.get("targets", [])
        n = data.get("n", 0)
        if not targets:
            return f"n={n} (no targets)"
        tgt_str = "  ".join(
            f"T{i+1}({t.get('x',0):+5d},{t.get('y',0):+5d}) spd={t.get('spd',0):+4d}"
            for i, t in enumerate(targets)
        )
        return f"n={n}  {tgt_str}"

    elif subtopic == "devices":
        wc = data.get("wifi_count", 0)
        bc = data.get("ble_count", 0)
        return f"wifi={wc} ble={bc} total={wc+bc}"

    elif subtopic == "environment":
        return (f"temp={data.get('temp_c','?')}°C  "
                f"hum={data.get('humidity','?')}%  "
                f"press={data.get('pressure_hpa','?')}hPa  "
                f"gas={data.get('gas_ohms','?')}Ω  "
                f"Δp={data.get('pressure_delta','?')}hPa")

    elif subtopic == "acoustic":
        imp = "⚡" if data.get("impulsive") else ""
        return (f"rms={data.get('rms_db','?')}dB  "
                f"peak={data.get('peak_db','?')}dB  "
                f"ambient={data.get('ambient_db','?')}dB {imp}")

    elif subtopic == "status":
        return (f"up={data.get('uptime_s','?')}s  "
                f"heap={data.get('heap_free','?')}  "
                f"rssi={data.get('wifi_rssi','?')}dBm")

    else:
        return json.dumps(data, separators=(",", ":"))


# ============================================================
# Main
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Sentinel Node Monitor")
    parser.add_argument("--broker", default="localhost", help="MQTT broker IP")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT port")
    parser.add_argument("--udp-port", type=int, default=5005, help="UDP listen port")
    args = parser.parse_args()

    print(f"\n{C_BOLD}{'='*60}")
    print(f"  SENTINEL MONITOR")
    print(f"  MQTT: {args.broker}:{args.mqtt_port}  |  UDP: :{args.udp_port}")
    print(f"{'='*60}{C_RESET}\n")

    # Start MQTT in background
    mqtt_listener(args.broker, args.mqtt_port)

    # Run UDP in foreground (blocks)
    try:
        udp_listener(args.udp_port)
    except KeyboardInterrupt:
        print(f"\n{C_BOLD}[EXIT] Monitor stopped{C_RESET}")


if __name__ == "__main__":
    main()
