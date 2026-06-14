# LiDAR Setup & Calibration Guide

## Hardware: YDLiDAR X4 Pro on ESP32-S3 (node-01)

### Wiring
| Signal | ESP32-S3 Pin | Notes |
|--------|-------------|-------|
| TX → LiDAR RX | GPIO15 | UART1 |
| RX ← LiDAR TX | GPIO16 | UART1 |
| Motor PWM | GPIO4 | 25kHz, duty 200/255 |
| Power | Separate 5V | Do NOT power from ESP32 |

### Firmware Config (config.h)
- `LIDAR_BAUD`: 128000
- `LIDAR_MIN_RANGE`: 120mm (filters near-field noise)
- `LIDAR_MOTOR_DUTY`: 200 (default speed)
- `LIDAR_PUBLISH_S`: 1 (1 Hz MQTT output)

### MQTT Output
Topic: `home/sentinel/node-01/lidar`

Payload: 12 sectors × 30° each, with min distance and hit count per sector.

---

## Orientation

The X4 Pro's **0° (front)** faces the direction of the motor. Per the datasheet, the zero angle is "directly in front of the motor."

### Finding 0° Empirically

If you can't find the arrow marking on the unit:

1. Run the sector monitor:
```bash
mosquitto_sub -h <broker-ip> -t "home/sentinel/node-01/lidar" | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        d = json.loads(line)
        active = [f'S{z[\"sector\"]}:{z[\"min_mm\"]}mm' for z in d['zones'] if z['min_mm'] >= 120]
        print(' | '.join(active) if active else '-- empty --')
    except: pass
"
```

2. Hold a book or clipboard ~150mm from one side of the LiDAR.
3. The sector that shows ~150mm is the direction you're blocking.
4. Repeat for 2-3 sides to confirm the full orientation.

### Current Office Placement (node-01)
- **Location**: Right side of desk
- **Motor faces**: Toward primary desk area (south toward office interior)
- **Cable exits**: To the left (as viewed from seated position)

---

## Sector Map — Office (node-01)

Sectors go **clockwise from above**, starting at 0° (toward primary desk).

| Sector | Angle | Label | Baseline | Type | What it sees |
|--------|-------|-------|----------|------|-------------|
| S0 | 0°–30° | primary_chair | 916mm | seat | Primary desk occupant seated |
| S1 | 30°–60° | desk_edge_left | 916mm | furniture | Left desk edge / torso |
| S2 | 60°–90° | monitor_wall | 604mm | wall | Monitor or close wall — always blocked |
| S3 | 90°–120° | kitchen_wall | 1584mm | wall | Wall/counter at kitchen boundary |
| S4 | 120°–150° | garage_kitchen_wall | 800mm | wall | Wall between garage and kitchen |
| S5 | 150°–180° | back_wall | 1680mm | wall | Garage wall behind desk (far) |
| S6 | 180°–210° | back_wall_close | 736mm | wall | Garage wall directly behind LiDAR |
| S7 | 210°–240° | back_right_wall | 856mm | wall | Wall/corner behind desk right |
| S8 | 240°–270° | secondary_desk | 1052mm | seat | Secondary desk area |
| S9 | 270°–300° | bathroom_door | — | opening | Opening toward bathroom / front door |
| S10 | 300°–330° | front_door_hall | ~5600mm | opening | Front door hallway — deep open space |
| S11 | 330°–360° | traffic_lane | ~2600mm | traffic | Main walking path — high variability |

### Key Observations
- **S2** is always ~604mm — permanently blocked by a monitor or wall. Can be ignored for presence detection.
- **S9/S10** rarely appear in filtered output — they look through the office opening into deep space (bathroom, front door, hallway). When someone walks through, these sectors briefly show readings.
- **S11** is the most useful traffic indicator — it's the walking path between both desks. When someone leaves the office, S11 briefly drops then jumps to 7500mm+ (seeing through the house).
- **S0** drops from ~916mm when primary occupant is absent, returns when they sit down.
- **S8** tracks secondary desk presence similarly.

---

## Calibration Walkthrough

### What We Did (2026-03-19)

1. **Hand test**: Held hand in front of LiDAR at ~150mm from multiple angles. Confirmed:
   - S0/S11 boundary = toward primary desk (motor facing user)
   - S7/S8 = toward right/back
   - S5/S6 = toward back wall

2. **Walk-around test**: Occupant walked through the space while monitoring sectors:
   - Stood up → S11 dropped to ~1100mm (body crossed traffic lane)
   - Walked to kitchen → S11 jumped to ~7600mm (nobody blocking, LiDAR sees through house)
   - Walked to bathroom/front door → S11 dropped again, S9/S10 briefly appeared
   - Walked to secondary desk → S8 region changed
   - Sat back down → S0 returned to ~916mm

3. **Baseline stability**: S0–S8 are rock-solid (±5mm std dev). S11 varies widely due to the open traffic lane.

---

## Automated Calibration

For new spaces or if you move the LiDAR, run:

```bash
cd ~/Presence
python3 scripts/lidar_calibrate.py --mqtt-host <broker-ip>
```

The script walks you through:
1. **Baseline** — empty room, captures averages for all 12 sectors
2. **Landmarks** — walk to each location, it detects which sectors change
3. **Review** — auto-labels sectors, you can override
4. **Export** — writes `sentinel/room_config.json`

---

## Configuration Files

| File | Purpose |
|------|---------|
| `sentinel_node/config.h` | ESP32 firmware — pins, baud, timing |
| `sentinel_node/ydlidar.h` | LiDAR parser — protocol, sectors, motor control |
| `sentinel/room_config.json` | Sector-to-room mapping (edit this for layout changes) |
| `sentinel_config.json` | Node-to-zone mapping, known devices |
| `scripts/lidar_calibrate.py` | Interactive calibration tool |

---

## Pipeline

```
YDLiDAR X4 Pro → UART1 → ESP32-S3 (ydlidar.h parser)
  → MQTT: home/sentinel/node-01/lidar
    → node_adapter.py → sentinel/sensors/office/lidar/raw
      → fusion service (weight 0.75)
        → zone occupancy
```

---

## Known Issues

- **LD2450 radar**: `frames=0` — needs separate debug session. Radar UART2 not producing valid frames despite being wired and enabled.
- **S2 permanently blocked**: Monitor/wall at 604mm. Should be in `ignore_sectors` for presence logic.
- **S5 oscillates** between ~1412mm and ~1680mm — possibly the LiDAR catching an edge of furniture vs wall at that angle. Baseline should use the higher value.

---

## Adding a New Room / Node

1. Place the LiDAR in the new room
2. Update `config.h` with the new `NODE_ID`
3. Add the node to `sentinel_config.json` zones
4. Run `lidar_calibrate.py` to generate a new `room_config.json`
5. Copy the config to the sentinel service directory
6. The node_adapter and fusion service will pick it up automatically
