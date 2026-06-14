# Hardware Inventory

## In Hand / Active
| Item | Details | Role | IP |
|---|---|---|---|
| NVIDIA Jetson Orin Nano | <keep-host> | AI hub, MQTT broker, n8n, CSI aggregator | 192.168.1.X |
| Raspberry Pi 4 4GB | <broker-host> | Camera brain, face recognition, PC control, MQTT broker | 192.168.1.X |
| BAKODELOP ESP32-S3 N16R8 (x4) | Dual USB-C, 16MB flash, 8MB PSRAM, USB-CDC, NO 5V output | Sentinel nodes — radar + scan + env sensors | 192.168.1.X (node-01) |
| HLK-LD2450 24GHz radar | Multi-target tracking, 3 targets, X/Y/speed | Per-zone presence + motion | UART → ESP32-S3 |
| MicroSD 32GB+ Class 10 | In Pi | Pi OS storage | — |

## Ordered / Pending
| Item | Details | Role |
|---|---|---|
| Arducam OV5647 Auto IR-CUT | 15-pin CSI ribbon | Day/night face recognition |
| Seeed XIAO ESP32S3 Sense | Entry camera | Wireless entry snapshot |

## To Order
| Item | Model | Qty | Approx Cost | Purpose |
|---|---|---|---|---|
| Desk vitals sensor | Seeed MR60BHA2 | 1 | ~$23 | Breathing rate + heart rate at desk |
| BME688 breakout | Adafruit | 1+ | ~$20 | Temperature, humidity, pressure, VOC |
| SPH0645 I2S mic | Adafruit | 1+ | ~$7 | Acoustic presence (Tier 1 nodes) |

## Network
| Device | IP | MAC | Notes |
|---|---|---|---|
| <keep-host> (Jetson) | 192.168.1.X | — | AI hub |
| <broker-host> (Pi 4) | 192.168.1.X | — | MQTT broker |
| PC (wake target) | 192.168.1.X | XX:XX:XX:XX:XX:XX | WoL target |
| ESP32-S3 node-01 | 192.168.1.X | — | WiFi: "YOUR_SSID" (hidden), COM14 |

## LD2450 Wiring (per node)
**IMPORTANT:** ESP32-S3 (BAKODELOP N16R8) has NO 5V output pin. LD2450 requires
separate 5V power from a cut USB cable or USB breakout board.

HKJL V1.1 breakout header (dual-row, 4 pairs):
```
5V    RX
3.3V  TX
PA9   DP
GND   DM
```

Wiring:
```
LD2450      →    ESP32-S3
5V          →    Separate USB 5V (cut cable red wire)
GND         →    ESP32 GND (shared ground)
TX          →    GPIO18 (RX on ESP32, UART2)
RX          →    GPIO17 (TX on ESP32, UART2)
```

Notes:
- LD2450 UART uses sign-magnitude encoding (bit15=sign, bits 14-0=magnitude)
- UART runs at 256000 baud, setRxBufferSize(512) required before Serial2.begin()
- LD2450 power must be connected before or during ESP32 boot to avoid USB-CDC issues

## YDLIDAR YDX4-Pro Wiring (Tier 2+ nodes)
**Power:** 5V via EAI Radar_Con V1.3.5 breakout board (USB_PWR port) or separate USB supply.

EAI Radar_Con V1.3.5 UART header:
```
M_CTR  GND  TX→  ←RX  +5V
```

Wiring:
```
YDX4-Pro (via breakout)  →    ESP32-S3
5V                       →    Separate USB 5V (breakout USB_PWR)
GND                      →    ESP32 GND (shared ground)
TX                       →    GPIO16 (RX on ESP32, UART1)
RX                       →    GPIO15 (TX on ESP32, UART1)
M_CTR                    →    GPIO4 (PWM motor control)
```

Notes:
- YDLIDAR protocol at 128000 baud (verify with datasheet)
- Motor must spin before scan data is produced
- EAI breakout USB_DATA port is for direct PC connection (not used with ESP32)

## BME688 Wiring (Tier 1 nodes)
```
BME688      →    ESP32-S3
VCC         →    3.3V
GND         →    GND
SDA         →    GPIO8
SCL         →    GPIO9
```

## SPH0645 Wiring (Tier 1 nodes)
```
SPH0645     →    ESP32-S3
VCC         →    3.3V
GND         →    GND
BCLK        →    GPIO5
LRCK        →    GPIO6
DOUT        →    GPIO7
```

## LED
```
Onboard LED →    GPIO48 (status/identify)
```
