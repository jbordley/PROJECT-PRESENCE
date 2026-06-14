# Sensor Architecture

## Why not RuView?

RuView (WiFi DensePose) was the original sensing backbone. It was determined to be non-functional — community audit confirmed fake/hardcoded CSI data, no trained models, and a simulation-only pipeline. The simulate mode works; real hardware does not.

The underlying physics is real. ESP32-S3 CSI sensing is legitimate and documented by Espressif. RuView just didn't implement it. We're building the real version ourselves.

## Sensing Layer

### Zone: Desk
| Sensor | Purpose | Range |
|---|---|---|
| Seeed MR60BHA2 (60GHz FMCW) | Breathing rate + heart rate | 1.5m for vitals, 6m for presence |
| ESP32-S3 + HLK-LD2450 | Multi-target tracking (3 targets, X/Y/speed) | 6m |
| ESP32-S3 WiFi CSI | Experimental — presence, breathing, HR | Room-scale (research track) |

### Zone: Entry / Foyer
| Sensor | Purpose |
|---|---|
| ESP32-S3 + HLK-LD2450 | Presence + motion — primary alarm trigger |
| Seeed XIAO ESP32S3 Sense | Entry snapshot on presence event |

### Zone: Living Room
| Sensor | Purpose |
|---|---|
| ESP32-S3 + HLK-LD2450 | Presence + motion |

## LD2450 Specs
- 24GHz multi-target tracking radar
- Up to 3 simultaneous targets with X/Y position (mm) and speed (cm/s)
- Detection range: up to 6m
- UART @ 256000 baud, binary frame protocol
- Frame: header `0xAA 0xFF 0x03 0x00` → 3x 8-byte target blocks → tail `0x55 0xCC`
- Connects to ESP32-S3 via UART2 (GPIO17 TX, GPIO18 RX)

## MR60BHA2 Specs
- 60GHz FMCW radar
- Breathing rate detection within 1.5m
- Heart rate detection within 1.5m
- Static presence detection to 6m
- Built-in XIAO ESP32C6 — WiFi direct, ESPHome pre-flashed
- ~$23

## ESP32-S3 CSI Capability
The BAKODELOP N16R8 captures real CSI data at ~20Hz across 56-128 subcarriers. This is genuine hardware capability documented by Espressif. The signal processing pipeline (Phase B-E firmware) extracts:
- **Phase B** — presence from amplitude variance
- **Phase C** — breathing rate via 0.1-0.5Hz bandpass + FFT
- **Phase D** — heart rate via 0.8-2.0Hz bandpass (harder, weaker signal)
- **Phase E** — person RF fingerprinting (research)
