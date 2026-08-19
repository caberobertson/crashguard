/*
 * ============================================================================
 *  CrashGuard configuration — every tunable in one place
 * ============================================================================
 */
#pragma once

/* ---------------- Transport: pick ONE primary ---------------- */
#define USE_BLE        1     /* BLE to the laptop bridge (default) */
#define USE_WIFI_HTTP  0     /* board joins hotspot, talks to server directly */

/* Wi-Fi mode settings (only used when USE_WIFI_HTTP = 1) */
#define WIFI_SSID        "your-hotspot"
#define WIFI_PASS        "your-password"
/* Base URL of the laptop running call_server.py, reachable on the SAME
 * hotspot network. Find the laptop's IP with ipconfig / ifconfig. */
#define SERVER_BASE_URL  "http://192.168.1.50:5000"

/* BLE identity (must match host/bridge_ble.py) */
#define BLE_DEVICE_NAME  "MYOSA-CrashGuard"
#define BLE_SVC_UUID     "b10e0001-c0de-4c9a-8a7e-3b2f1d4e5a6c"
#define BLE_EVT_UUID     "b10e0002-c0de-4c9a-8a7e-3b2f1d4e5a6c"  /* crash alert  */
#define BLE_TLM_UUID     "b10e0003-c0de-4c9a-8a7e-3b2f1d4e5a6c"  /* live status  */
#define BLE_CMD_UUID     "b10e0004-c0de-4c9a-8a7e-3b2f1d4e5a6c"  /* phone cmds   */

/* If the bridge stops sending heartbeats for this long, assume it died (it was
 * killed without a clean BLE disconnect) and drop the link so a NEW bridge can
 * connect without power-cycling the board. Must be several times the bridge's
 * ping interval (3 s). */
#define BLE_LINK_TIMEOUT_MS  12000

/* ---------------- Crash detection ---------------- */
#define SAMPLE_HZ            100    /* accel sampling rate */
/* ---- ACCELEROMETER SCALING (true g) ----
 * We read raw int16 counts with getAccel() and scale them ourselves, because
 * the MYOSA library's own scale factor is 2x too large for every range.
 * MPU6050 sensitivity (LSB per g):  +/-2g=16384  +/-4g=8192  +/-8g=4096  +/-16g=2048
 * ACCEL_LSB_PER_G must match the range set by setFullScaleAccelRange(). */
#define ACCEL_LSB_PER_G      2048.0f   /* for MPU_ACCEL_CONFIG_FS_SEL_16g */
#define SENSOR_RANGE_G       16.0f     /* hardware ceiling per axis */

/* Threshold is DYNAMIC acceleration in TRUE g: the gravity vector is removed,
 * so this reads 0.00 g at rest in ANY orientation and the number is pure
 * impact. Reference points:
 *   sitting still, any orientation .... ~0.0 g
 *   being picked up / rotated by hand .. under 2 g (rotation can reach 2 g)
 *   driving an RC car, bumps ........... ~1-3 g
 *   deliberate crash into a box ........ ~4-15 g
 * Keep this ABOVE 2 so simply handling or rotating the board never fires it.
 * Impacts that rail the sensor are caught separately by saturation detection. */
#define CRASH_G_THRESHOLD    5.0f

/* ---- DELTA-V GATE (this is what rejects taps, knocks and grazes) ----
 * Peak g alone cannot tell a sharp knock from a collision: a fingernail tap on
 * a rigid board genuinely spikes past 15 g for a few milliseconds. Real airbag
 * controllers therefore integrate acceleration to get VELOCITY CHANGE, because
 * a crash is a sustained one-way deceleration while a tap is a brief
 * oscillation that integrates to almost nothing.
 *
 * Each axis is integrated separately WITH SIGN over a sliding window, so
 * ringing cancels itself out and only genuine one-way deceleration builds up.
 * A crash must exceed BOTH the g threshold above and the delta-V below. */
#define USE_DELTA_V_GATE     1      /* 0 = peak-g only (old, twitchy behaviour) */
#define DELTA_V_WINDOW_MS    150    /* crash pulses last ~50-150 ms; taps ~5-10 ms */
#define DELTA_V_THRESHOLD    2.0f   /* m/s of velocity change. RC car into a box
                                       typically 3-8 m/s; a tap is under 0.5 m/s.
                                       Raise if driving still trips it. */
#define IMPACT_MIN_SAMPLES   2      /* consecutive samples over threshold. At 100 Hz a
                                       real crash easily lasts 2 samples (20 ms); the high
                                       threshold (not this) is what rejects taps/vibration. */
#define ARM_DELAY_MS         4000   /* ignore impacts right after boot */
#define BASELINE_SAMPLES     100    /* boot-time "what does 1 g read as" */

/* ---------------- Occupant interaction ---------------- */
#define COUNTDOWN_SECONDS    10     /* cancel window after impact */
#define PIN_BUTTON           0      /* onboard backup cancel (BOOT btn). */
#define LONGPRESS_MS         1500   /* hold to re-arm after an alert */
#define PIN_BUZZER           12     /* passive piezo; set -1 if none wired */

/* ---------------- Status streaming (feeds the PHONE DASHBOARD) -------- */
/* Live state is pushed off-board and rendered at http://<laptop>:5000/
 * on the phone. STATUS_HZ is how often {G, peak, state, countdown} goes out
 * over BLE notify or Wi-Fi POST. 5 Hz is smooth and cheap. */
#define STATUS_HZ            5

/* ---------------- Optional OLED (bench debugging only) ---------------- */
/* The demo shows everything on the phone dashboard; the car is moving and
 * nobody can read a 1" screen on it. Set to 1 only if the OLED module is
 * connected and you want a local readout on the bench. */
#define USE_OLED             0
#define SCREEN_WIDTH         128
#define SCREEN_HEIGHT        64

/* ---------------- Optional proximity warning ---------------- */
#define USE_PROXIMITY        0
#define PROX_WARN_THRESHOLD  5.0f
#define PROX_POLL_HZ         5

/* ---------------- Misc ---------------- */
#define I2C_CLOCK_HZ         100000  /* MYOSA examples run the bus at 100 kHz */
#define TELEMETRY_HZ         20      /* serial "T," lines for calibration */
