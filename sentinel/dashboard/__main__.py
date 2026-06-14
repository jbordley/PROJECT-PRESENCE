"""
Run sentinel dashboard:
  python -m sentinel.dashboard                              → default (mqtt=<broker-ip>, port=8080)
  python -m sentinel.dashboard --mqtt-host <broker-ip>      → explicit MQTT host
  python -m sentinel.dashboard --port 9090                  → custom web port
"""

from sentinel.dashboard.service import main

main()
