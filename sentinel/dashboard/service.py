"""
SENTINEL Dashboard Service
============================
FastAPI + WebSocket server that bridges MQTT → browser.

Subscribes to all sentinel/ topics and pushes JSON to connected WebSocket clients.
Serves static dashboard HTML from the same port.

Run: python -m sentinel.dashboard --mqtt-host <broker-ip> --port 8080
"""

import argparse
import asyncio
import json
import logging
import time
import threading
from pathlib import Path
from typing import Set

import paho.mqtt.client as mqtt

log = logging.getLogger("sentinel.dashboard")


class MQTTBridge:
    """Subscribes to MQTT topics and forwards messages to an async queue."""

    # Topics to subscribe to (covers all sentinel layers)
    SUBSCRIPTIONS = [
        ("sentinel/sensors/+/+/raw", 0),       # raw sensor data
        ("sentinel/context/+/state", 0),        # zone states
        ("sentinel/context/+/occupancy", 0),    # zone occupancy
        ("sentinel/context/home/narrative", 0), # narrative (single source of truth)
        ("sentinel/identity/+/location", 0),    # person locations
        ("sentinel/identity/+/vitals", 0),      # person vitals
        ("sentinel/system/+/health", 0),        # node health
        ("sentinel/system/brain/status", 0),    # brain heartbeat
        ("sentinel/system/alerts/+", 0),        # alerts
        ("sentinel/system/meta/#", 0),          # meta-reasoner
    ]

    def __init__(self, host: str, port: int, loop: asyncio.AbstractEventLoop,
                 queue: asyncio.Queue):
        self.host = host
        self.port = port
        self._loop = loop
        self._queue = queue
        self._client = mqtt.Client(
            client_id="sentinel-dashboard",
            protocol=mqtt.MQTTv311,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._connected = False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected to %s:%d", self.host, self.port)
            self._connected = True
            client.subscribe(self.SUBSCRIPTIONS)
            log.info("Subscribed to %d topic patterns", len(self.SUBSCRIPTIONS))
        else:
            log.error("MQTT connect failed: rc=%d", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%d), will reconnect", rc)

    def _on_message(self, client, userdata, msg):
        """Forward MQTT message to async queue (thread-safe)."""
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            # Try to parse as JSON, fall back to string
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                data = payload

            envelope = {
                "topic": msg.topic,
                "data": data,
                "ts": time.time(),
            }
            self._loop.call_soon_threadsafe(self._queue.put_nowait, envelope)
        except Exception as e:
            log.debug("Message parse error on %s: %s", msg.topic, e)

    def start(self):
        """Connect and start MQTT loop in background thread."""
        self._client.connect_async(self.host, self.port, keepalive=60)
        self._client.loop_start()
        log.info("MQTT bridge started → %s:%d", self.host, self.port)

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()
        log.info("MQTT bridge stopped")

    @property
    def connected(self) -> bool:
        return self._connected


class DashboardServer:
    """
    Async WebSocket server + static file server.

    Uses only asyncio + stdlib for minimal dependencies.
    WebSocket implementation via FastAPI/Starlette (lightweight).
    """

    def __init__(self, mqtt_host: str, mqtt_port: int, web_port: int):
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.web_port = web_port
        self._clients: Set[asyncio.Queue] = set()
        self._queue: asyncio.Queue = None
        self._bridge: MQTTBridge = None
        # Latest state cache — new clients get current state immediately
        self._state_cache: dict = {}

    async def run(self):
        """Main entry point — start MQTT bridge + web server."""
        try:
            from starlette.applications import Starlette
            from starlette.routing import Route, WebSocketRoute
            from starlette.responses import HTMLResponse
            from starlette.websockets import WebSocket
            import uvicorn
        except ImportError:
            log.error(
                "Dashboard requires: pip install 'uvicorn[standard]' starlette\n"
                "  Or: pip install 'sentinel[dashboard]'"
            )
            raise SystemExit(1)

        loop = asyncio.get_event_loop()
        self._queue = asyncio.Queue(maxsize=1000)
        self._bridge = MQTTBridge(
            self.mqtt_host, self.mqtt_port, loop, self._queue
        )

        # Load HTML template
        html_path = Path(__file__).parent / "index.html"
        html_content = html_path.read_text()

        async def homepage(request):
            return HTMLResponse(html_content)

        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            client_queue = asyncio.Queue(maxsize=500)
            self._clients.add(client_queue)
            log.info("WebSocket client connected (%d total)", len(self._clients))

            # Send cached state so new clients see current data
            if self._state_cache:
                try:
                    await websocket.send_json({
                        "type": "state_cache",
                        "data": self._state_cache,
                        "ts": time.time(),
                    })
                except Exception:
                    pass

            try:
                while True:
                    msg = await client_queue.get()
                    try:
                        await websocket.send_json(msg)
                    except Exception:
                        break
            finally:
                self._clients.discard(client_queue)
                log.info("WebSocket client disconnected (%d remaining)",
                         len(self._clients))

        async def health(request):
            from starlette.responses import JSONResponse
            return JSONResponse({
                "service": "sentinel-dashboard",
                "mqtt_connected": self._bridge.connected if self._bridge else False,
                "ws_clients": len(self._clients),
                "cached_topics": len(self._state_cache),
                "uptime_s": time.time() - self._start_time,
            })

        app = Starlette(
            routes=[
                Route("/", homepage),
                Route("/health", health),
                WebSocketRoute("/ws", websocket_endpoint),
            ],
        )

        self._start_time = time.time()

        # Start MQTT bridge
        self._bridge.start()

        # Start message fanout in background
        fanout_task = asyncio.create_task(self._fanout_loop())

        # Run web server
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.web_port,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(config)

        log.info("Dashboard serving on http://0.0.0.0:%d", self.web_port)
        try:
            await server.serve()
        finally:
            fanout_task.cancel()
            self._bridge.stop()

    async def _fanout_loop(self):
        """Read from MQTT queue, cache state, broadcast to all WebSocket clients."""
        while True:
            msg = await self._queue.get()
            topic = msg.get("topic", "")

            # Cache latest value per topic for new client catch-up
            self._state_cache[topic] = msg

            # Classify message for the frontend
            msg["layer"] = self._classify_layer(topic)

            # Broadcast to all connected clients
            dead = []
            for client_queue in self._clients:
                try:
                    client_queue.put_nowait(msg)
                except asyncio.QueueFull:
                    dead.append(client_queue)

            for d in dead:
                self._clients.discard(d)

    @staticmethod
    def _classify_layer(topic: str) -> str:
        """Classify topic into UI layer for frontend routing."""
        if topic.startswith("sentinel/sensors/"):
            return "sensor"
        elif topic.startswith("sentinel/context/"):
            if "narrative" in topic:
                return "narrative"
            return "context"
        elif topic.startswith("sentinel/identity/"):
            return "identity"
        elif topic.startswith("sentinel/system/"):
            if "alert" in topic:
                return "alert"
            return "system"
        return "unknown"


def parse_args():
    parser = argparse.ArgumentParser(description="Sentinel Dashboard")
    parser.add_argument("--mqtt-host", default="localhost",
                        help="MQTT broker host (default: localhost)")
    parser.add_argument("--mqtt-port", type=int, default=1883,
                        help="MQTT broker port (default: 1883)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Web server port (default: 8080)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    server = DashboardServer(
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        web_port=args.port,
    )
    asyncio.run(server.run())
