#!/usr/bin/env python3
"""
TC001 Y16 Raw Frame Analyzer — Phase 2
We know the header is 2664 bytes. Now figure out the temperature encoding.

Usage: python3 scripts/tc001_y16_probe.py [device_index]
"""
import sys
import numpy as np
import cv2

DEV = int(sys.argv[1]) if len(sys.argv) > 1 else 1
W, H = 4, 12621
THERMAL_W, THERMAL_H = 256, 192
THERMAL_BYTES = THERMAL_W * THERMAL_H * 2
HEADER_OFFSET = 2664

print(f"Opening /dev/video{DEV} at {W}x{H}...")
cap = cv2.VideoCapture(DEV, cv2.CAP_V4L2)
cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)

for _ in range(5):
    cap.grab()
    cap.retrieve()

cap.grab()
ret, frame = cap.retrieve()
cap.release()

if not ret or frame is None:
    print("ERROR: Failed to capture")
    sys.exit(1)

raw = frame.flatten()
print(f"Total bytes: {len(raw)}, header: {HEADER_OFFSET}, thermal: {THERMAL_BYTES}")

chunk = raw[HEADER_OFFSET:HEADER_OFFSET + THERMAL_BYTES]
u16 = np.frombuffer(chunk.tobytes(), dtype=np.uint16).reshape((THERMAL_H, THERMAL_W))
s16 = u16.view(np.int16)

print(f"\nRaw uint16 — median: {np.median(u16):.0f}, min: {np.min(u16)}, max: {np.max(u16)}")
print(f"Raw int16  — median: {np.median(s16):.0f}, min: {np.min(s16)}, max: {np.max(s16)}")

print("\n=== Temperature Interpretations ===\n")

def show(label, temps):
    print(f"  {label}")
    print(f"    Median: {np.median(temps):.1f}°C  Min: {np.min(temps):.1f}°C  Max: {np.max(temps):.1f}°C")
    ok = 15 < np.median(temps) < 35 and np.min(temps) > -10 and np.max(temps) < 80
    print(f"    {'>>> LOOKS CORRECT <<<' if ok else '(unlikely)'}\n")

show("1. centi-Kelvin: raw/100 - 273.15",
     u16.astype(np.float32) / 100.0 - 273.15)

show("2. Subtract 0x8000 bias, centi-Kelvin: (raw-32768)/100 - 273.15",
     (u16.astype(np.float32) - 32768) / 100.0 - 273.15)

show("3. Subtract 0x8000, deci-Kelvin: (raw-32768)/10 - 273.15",
     (u16.astype(np.float32) - 32768) / 10.0 - 273.15)

show("4. InfiRay: raw/64 - 273.15",
     u16.astype(np.float32) / 64.0 - 273.15)

show("5. Offset-Celsius: (raw - 27315) / 100",
     (u16.astype(np.float32) - 27315.0) / 100.0)

show("6. Signed int16 / 100 (direct centi-°C)",
     s16.astype(np.float32) / 100.0)

show("7. Signed int16 / 10 (direct deci-°C)",
     s16.astype(np.float32) / 10.0)

show("8. (raw - 27315) / 10",
     (u16.astype(np.float32) - 27315.0) / 10.0)

show("9. raw / 10 - 273.15 (deci-Kelvin)",
     u16.astype(np.float32) / 10.0 - 273.15)

show("10. raw * 0.04 - 273.15 (InfiRay alt)",
     u16.astype(np.float32) * 0.04 - 273.15)

show("11. raw * 0.01 (raw IS centi-°C, no Kelvin offset)",
     u16.astype(np.float32) * 0.01)

show("12. (raw - 32768) * 0.04 (bias + scale)",
     (u16.astype(np.float32) - 32768) * 0.04)

show("13. (raw - 32768) * 0.01 (bias then centi-°C)",
     (u16.astype(np.float32) - 32768) * 0.01)

# Value histogram
print("=== Value distribution (uint16) ===")
hist, edges = np.histogram(u16, bins=20)
for i in range(len(hist)):
    bar = "#" * min(hist[i] // 100, 50)
    print(f"  {int(edges[i]):>6}-{int(edges[i+1]):>6}: {hist[i]:>6}  {bar}")

# First row
print(f"\nFirst 10 pixels: {list(u16[0, :10])}")
print(f"Center pixel [96,128]: {u16[96, 128]}")
print(f"Corner pixels: TL={u16[0,0]} TR={u16[0,255]} BL={u16[191,0]} BR={u16[191,255]}")
