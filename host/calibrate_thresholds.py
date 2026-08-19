"""
calibrate_thresholds.py — pick CRASH_G_THRESHOLD with data, not vibes.

The firmware streams telemetry lines over USB serial:
    T,<millis>,<G>,<peakG>,<state>

Protocol (see TUTORIAL.md Stage 6):
  1. Record ~3 runs of NORMAL driving (hard starts, stops, turns, small bumps):
        python calibrate_thresholds.py --port COM5 --seconds 30 --label normal
  2. Record ~3 CRASH runs (drive into a cardboard box / wall):
        python calibrate_thresholds.py --port COM5 --seconds 15 --label crash
  3. Set CRASH_G_THRESHOLD in config.h between the two:
        >= 1.5 x (max normal G)   and   <= 0.7 x (typical crash peak)

Find your port: Arduino IDE > Tools > Port  (COMx on Windows,
/dev/ttyUSB0 or /dev/cu.usbserial-* on Linux/macOS).
"""

import argparse
import csv
import statistics
import time

import serial  # pyserial


def main() -> None:
    ap = argparse.ArgumentParser(description="CrashGuard threshold calibration")
    ap.add_argument("--port", required=True, help="serial port, e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=int, default=30, help="capture duration")
    ap.add_argument("--label", default="run", help="'normal' or 'crash' (for the report)")
    ap.add_argument("--out", default=None, help="optional CSV file for the raw samples")
    args = ap.parse_args()

    print(f"Opening {args.port} @ {args.baud} ... drive when ready!")
    samples: list[float] = []
    rows: list[tuple[int, float]] = []

    with serial.Serial(args.port, args.baud, timeout=1) as ser:
        ser.reset_input_buffer()
        t_end = time.time() + args.seconds
        while time.time() < t_end:
            line = ser.readline().decode("utf-8", errors="replace").strip()
            if not line.startswith("T,"):
                if line.startswith(("EVT,", "ERR,")):
                    print(f"  {line}")
                continue
            try:
                _, ms, g, _peak, _state = line.split(",")
                g_val = float(g)
            except ValueError:
                continue
            samples.append(g_val)
            rows.append((int(ms), g_val))
            remaining = int(t_end - time.time())
            print(f"\r  samples={len(samples):5d}  G={g_val:5.2f}  "
                  f"max={max(samples):5.2f}  {remaining:3d}s left ", end="")

    print("\n")
    if not samples:
        print("No telemetry received. Is the CrashGuard sketch running? "
              "Close the Arduino Serial Monitor — only one program can own the port.")
        return

    mx = max(samples)
    p999 = statistics.quantiles(samples, n=1000)[-1] if len(samples) > 100 else mx
    print(f"===== {args.label.upper()} run report =====")
    print(f"  samples : {len(samples)}")
    print(f"  mean G  : {statistics.fmean(samples):.2f}")
    print(f"  p99.9 G : {p999:.2f}")
    print(f"  max G   : {mx:.2f}")

    if args.label.lower().startswith("n"):
        print(f"\n  -> normal driving: keep CRASH_G_THRESHOLD >= {1.5 * mx:.1f} "
              "(1.5x your max) to avoid false alarms.")
    else:
        print(f"\n  -> crash runs: keep CRASH_G_THRESHOLD <= {0.7 * mx:.1f} "
              "(0.7x your peak) so real hits always trigger.")

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["millis", "g"])
            w.writerows(rows)
        print(f"  raw samples saved to {args.out}")


if __name__ == "__main__":
    main()
