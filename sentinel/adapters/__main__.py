"""
Run sentinel adapters:
  python -m sentinel.adapters                              → Node adapter (default)
  python -m sentinel.adapters --csi                        → Both CSI + Node
  python -m sentinel.adapters --mqtt-host <broker-ip>      → explicit MQTT host
"""
import argparse
import logging
import signal
import sys
import threading

from sentinel.adapters.node_adapter import NodeAdapter
from sentinel.config import SentinelConfig, CONFIG_PATH


def main():
    parser = argparse.ArgumentParser(description="SENTINEL Adapters")
    parser.add_argument("--config", type=str, default=CONFIG_PATH)
    parser.add_argument("--mqtt-host", type=str, default=None,
                        help="Override MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, default=None,
                        help="Override MQTT broker port")
    parser.add_argument("--csi", action="store_true",
                        help="Also run CSI adapter (requires csi_bridge running)")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = SentinelConfig.load(args.config)
    if args.mqtt_host:
        config.mqtt.host = args.mqtt_host
    if args.mqtt_port:
        config.mqtt.port = args.mqtt_port

    adapters = []
    node = NodeAdapter(config)
    adapters.append(node)

    csi = None
    if args.csi:
        try:
            from sentinel.adapters.csi_adapter import CSIAdapter
            csi = CSIAdapter(config)
            adapters.append(csi)
        except Exception as e:
            logging.getLogger("sentinel.adapters").warning(
                "CSI adapter not available: %s", e)

    def shutdown(signum, frame):
        for a in adapters:
            a.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Run CSI adapter in background thread if enabled
    if csi:
        csi_thread = threading.Thread(target=csi.start, name="csi-adapter", daemon=True)
        csi_thread.start()

    # Node adapter runs in main thread
    node.start()


main()
