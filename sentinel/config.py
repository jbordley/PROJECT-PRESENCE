"""
SENTINEL Configuration
=======================
Installation-specific settings. Override via sentinel_config.json or environment vars.

Default config is <user>'s home setup:
  - Raspberry Pi 4 (<broker-host>, <host-ip>) runs Mosquitto broker + adapters + fusion
  - Jetson Orin Nano (<keep-host>, <keep-ip>) reserved for ML inference
  - ESP32-S3 nodes (BAKODELOP x4) are sensor nodes
"""

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger("sentinel.config")

# ── Default paths ─────────────────────────────────────────────────────────

CONFIG_PATH = os.environ.get(
    "SENTINEL_CONFIG",
    str(Path(__file__).resolve().parent.parent / "sentinel_config.json")
)


@dataclass
class MQTTConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    keepalive: int = 60
    client_id_prefix: str = "sentinel"
    username: Optional[str] = None
    password: Optional[str] = None
    # TLS fields for Stage 8.3 (secure comms)
    use_tls: bool = False
    ca_certs: Optional[str] = None
    certfile: Optional[str] = None
    keyfile: Optional[str] = None


@dataclass
class ZoneConfig:
    """Per-zone configuration."""
    name: str = ""
    nodes: list = field(default_factory=list)     # node_ids assigned to this zone
    adjacent_zones: list = field(default_factory=list)  # for transition handoffs
    polygon_ft: list = field(default_factory=list)  # [[x,y], ...] polygon vertices in feet


@dataclass
class NodeConfig:
    """Per-node configuration."""
    node_id: str = ""
    zone: str = ""
    sensors: list = field(default_factory=list)    # sensor types on this node
    position_ft: list = field(default_factory=list)  # [x, y] position in feet
    ip: Optional[str] = None


@dataclass
class SentinelConfig:
    """Top-level configuration."""
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)

    # Installation-specific
    home_name: str = "home"
    owner: str = "alice"

    # Zone definitions
    zones: dict = field(default_factory=lambda: {
        "office": ZoneConfig(
            name="office",
            nodes=["node_1"],
            adjacent_zones=["hallway"],
        ),
        "hallway": ZoneConfig(
            name="hallway",
            nodes=[],
            adjacent_zones=["office", "entry", "bedroom", "kitchen", "living_room", "bathroom"],
        ),
        "entry": ZoneConfig(
            name="entry",
            nodes=[],
            adjacent_zones=["hallway"],
        ),
        "bedroom": ZoneConfig(
            name="bedroom",
            nodes=[],
            adjacent_zones=["hallway", "bathroom"],
        ),
        "kitchen": ZoneConfig(
            name="kitchen",
            nodes=[],
            adjacent_zones=["hallway", "living_room"],
        ),
        "living_room": ZoneConfig(
            name="living_room",
            nodes=[],
            adjacent_zones=["hallway", "kitchen"],
        ),
        "bathroom": ZoneConfig(
            name="bathroom",
            nodes=[],
            adjacent_zones=["hallway", "bedroom"],
        ),
    })

    # Node registry
    nodes: dict = field(default_factory=lambda: {
        "node_1": NodeConfig(
            node_id="node_1",
            zone="office",
            sensors=["radar", "acoustic", "voc", "emrf", "csi"],
            ip=None,  # assigned by DHCP
        ),
        # Nodes 2-4: uncomment and assign zones when flashed
        # "node_2": NodeConfig(node_id="node_2", zone="hallway", sensors=["radar", "acoustic", "voc", "emrf"]),
        # "node_3": NodeConfig(node_id="node_3", zone="bedroom", sensors=["radar", "acoustic", "voc", "emrf"]),
        # "node_4": NodeConfig(node_id="node_4", zone="kitchen", sensors=["radar", "acoustic", "voc", "emrf"]),
    })

    # Known devices → person identity mapping
    # Structure: { "person_id": { "name": "Display Name", "devices": [{"mac": "AA:BB:...", "label": "phone", "type": "ble"}] } }
    known_devices: dict = field(default_factory=dict)

    # Known infrastructure devices → category mapping
    # Structure: { "category": [{"mac": "AA:BB:...", "label": "Device Name", "ip": "<device-ip>"}] }
    known_infrastructure: dict = field(default_factory=dict)

    # Brain config
    brain_tier: int = 3                # 1=node, 2=zone, 3=primary
    narrative_publish_interval: float = 1.0  # seconds
    health_publish_interval: float = 10.0

    # Reasoning loop intervals
    narrative_update_interval: float = 0.5   # how often brain re-evaluates narrative
    intent_inference_interval: float = 2.0   # how often intent layer runs

    def zone_polygons(self) -> dict:
        """Build {zone_id: [[x,y], ...]} polygon map for geometry lookups."""
        return {
            zid: zc.polygon_ft
            for zid, zc in self.zones.items()
            if zc.polygon_ft
        }

    def node_positions(self) -> dict:
        """Build {node_id: (x, y)} position map for trilateration."""
        return {
            nid: tuple(nc.position_ft)
            for nid, nc in self.nodes.items()
            if nc.position_ft and len(nc.position_ft) == 2
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def save(self, path: Optional[str] = None):
        path = path or CONFIG_PATH
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        log.info("Config saved to %s", path)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "SentinelConfig":
        path = path or CONFIG_PATH
        if not Path(path).exists():
            log.info("No config at %s, using defaults", path)
            return cls()

        with open(path) as f:
            data = json.load(f)

        cfg = cls()
        if "mqtt" in data:
            cfg.mqtt = MQTTConfig(**data["mqtt"])
        for key in ["home_name", "owner", "brain_tier",
                     "narrative_publish_interval", "health_publish_interval",
                     "narrative_update_interval", "intent_inference_interval"]:
            if key in data:
                setattr(cfg, key, data[key])

        if "zones" in data:
            cfg.zones = {
                k: ZoneConfig(**v) if isinstance(v, dict) else v
                for k, v in data["zones"].items()
            }
        if "nodes" in data:
            cfg.nodes = {
                k: NodeConfig(**v) if isinstance(v, dict) else v
                for k, v in data["nodes"].items()
            }
        if "known_devices" in data:
            cfg.known_devices = data["known_devices"]
        if "known_infrastructure" in data:
            cfg.known_infrastructure = data["known_infrastructure"]

        log.info("Config loaded from %s", path)
        return cfg

    def build_mac_identity_map(self) -> dict:
        """Build a MAC (uppercase) → {"person_id", "name", "label", "type"} lookup.

        Returns dict like:
          {"AA:BB:CC:DD:EE:FF": {"person_id": "alice", "name": "Alice", "label": "phone", "type": "ble"}}
        """
        mac_map = {}
        for person_id, person_data in self.known_devices.items():
            name = person_data.get("name", person_id)
            for dev in person_data.get("devices", []):
                mac = dev.get("mac", "").upper().strip()
                if mac and mac != "XX:XX:XX:XX:XX:XX":
                    mac_map[mac] = {
                        "person_id": person_id,
                        "name": name,
                        "label": dev.get("label", "unknown"),
                        "type": dev.get("type", "unknown"),
                    }
        return mac_map

    def build_ble_name_identity_map(self) -> dict:
        """Build a BLE device name (lowercase) → {"person_id", "name", "label"} lookup.

        Matches against the 'ble_name' field in known_devices config entries.
        BLE names are case-insensitive (lowered at lookup time).

        Returns dict like:
          {"alice's iphone": {"person_id": "alice", "name": "Alice", "label": "phone"}}
        """
        name_map = {}
        for person_id, person_data in self.known_devices.items():
            name = person_data.get("name", person_id)
            for dev in person_data.get("devices", []):
                ble_name = dev.get("ble_name", "").strip()
                if ble_name:
                    name_map[ble_name.lower()] = {
                        "person_id": person_id,
                        "name": name,
                        "label": dev.get("label", "unknown"),
                    }
        return name_map

    def build_infra_identity_map(self) -> dict:
        """Build a MAC (uppercase) → {"category", "label", "ip"} lookup for infrastructure devices.

        Returns dict like:
          {"AA:BB:CC:DD:EE:FF": {"category": "media", "label": "Sonos ZP100", "ip": "<device-ip>"}}
        """
        infra_map = {}
        for category, devices in self.known_infrastructure.items():
            for dev in devices:
                mac = dev.get("mac", "").upper().strip()
                if mac:
                    infra_map[mac] = {
                        "category": category,
                        "label": dev.get("label", "unknown"),
                        "ip": dev.get("ip", ""),
                    }
        return infra_map
