/*
 * ============================================================================
 *  CrashGuard — autonomous crash detection on the MYOSA platform
 *  MYOSA Event 6.0 (2026)
 *
 *  Team: Aidan Garcia, Jacob Randall, Cabe Robertson, Vu Ha Nguyen
 *  Faculty mentor: Dr. Lavrova — Klipsch School of ECE, New Mexico State Univ.
 * ============================================================================
 *
 *  WHAT THIS DOES
 *  --------------
 *  1. Continuously samples the MYOSA Accelerometer/Gyro module (MPU6050 @ 0x69).
 *  2. Normalizes acceleration magnitude against a boot-time baseline, so a
 *     reading of 1.00 G always means "sitting still" regardless of library
 *     scaling quirks.
 *  3. Streams live status (G, peak, state, countdown) off-board at STATUS_HZ.
 *     The host renders it as a PHONE DASHBOARD — the car is moving, so all
 *     results are shown on the phone, not on the board.
 *  4. When |a| exceeds CRASH_G_THRESHOLD -> 10-second cancel countdown with
 *     buzzer alarm. Cancel comes from the phone's big CANCEL button (or the
 *     onboard backup button).
 *  5. If the countdown expires -> emergency alert is pushed to the host, which
 *     places the AI phone call and speaks the occupant's preloaded profile.
 *
 *  HARDWARE (MYOSA Mini kit)
 *  -------------------------
 *   - MYOSA Motherboard (ESP32, Wi-Fi + Bluetooth)
 *   - Accelerometer + Gyroscope module  (MPU6050, I2C 0x69)
 *   - Add-ons: passive piezo buzzer on PIN_BUZZER, pushbutton on PIN_BUTTON
 *   - Optional: OLED module for bench debugging only (USE_OLED in config.h)
 *   - Optional: Light/Proximity/Gesture (APDS9960) for obstacle warnings
 *
 *  LIBRARIES
 *  ---------
 *  Official MYOSA libraries (AccelAndGyro; OLED only if USE_OLED):
 *      git clone https://github.com/myosa-sensors/arduino-libraries
 *      -> copy the folders into your Arduino/libraries directory.
 *  BLE support ships with the ESP32 Arduino core; nothing extra to install.
 *
 *  All tuning lives in config.h.
 * ============================================================================
 */

#include <Wire.h>
#include <AccelAndGyro.h>          // MYOSA official — MPU6050 wrapper (cm/s^2)
#include "config.h"

#if USE_OLED
#include <oled.h>                  // MYOSA official — SSD1306 + Adafruit_GFX
#endif

#if USE_PROXIMITY
#include <LightProximityAndGesture.h>  // MYOSA official — APDS9960 wrapper
#endif

#if USE_BLE
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#endif

#if USE_WIFI_HTTP
#include <WiFi.h>
#include <HTTPClient.h>
#endif

/* ------------------------------------------------------------------ */
/*  Devices                                                            */
/* ------------------------------------------------------------------ */
AccelAndGyro Ag;                              // defaults to I2C address 0x69

#if USE_OLED
oLed display(SCREEN_WIDTH, SCREEN_HEIGHT);    // 128x64, address 0x3C internally
#endif

#if USE_PROXIMITY
LightProximityAndGesture Lpg;
#endif

/* ------------------------------------------------------------------ */
/*  State machine                                                      */
/* ------------------------------------------------------------------ */
enum SysState : uint8_t {
  ST_MONITORING = 0,   // armed, watching G-forces
  ST_COUNTDOWN  = 1,   // impact seen — occupant can cancel (phone or button)
  ST_ALERT_SENT = 2,   // countdown expired — alert pushed to host
  ST_DISARMED   = 3    // brief "canceled" pause, then back to monitoring
};

/* remote commands (from the phone dashboard, relayed by the host) */
enum RemoteCmd : uint8_t { CMD_NONE = 0, CMD_CANCEL, CMD_REARM };

static SysState state          = ST_MONITORING;
static volatile RemoteCmd remoteCmd = CMD_NONE;

static float    baselineMag    = 1.0f;     // |gravity| in g at boot (sanity check, ~1.00)
static float    gravX = 0, gravY = 0, gravZ = 1.0f;   // gravity VECTOR at rest, in g
static float    liveG          = 0.0f;     // DYNAMIC acceleration in g (0 at rest)
static float    gHist[3]       = {0.0f, 0.0f, 0.0f};  // last 3 dynamic samples (median)
static uint8_t  gHistIdx       = 0;
static float    peakG          = 0.0f;     // session peak since last (re)arm
static float    crashPeakG     = 0.0f;     // peak captured for the alert payload
static float    crashDecelG    = 0.0f;     // deceleration along travel axis at impact
static char     crashAxis      = '?';      // dominant axis at impact: X, Y or Z
static bool     crashSaturated = false;    // sensor railed (impact exceeded +/-16 g)
static uint8_t  satCount       = 0;        // consecutive saturated samples
static uint8_t  overCount      = 0;        // consecutive samples over threshold
static uint32_t countdownEndMs = 0;
static uint32_t disarmSplashMs = 0;

/* button debounce / long-press (onboard backup) */
static bool     btnStable      = true;     // true = released (INPUT_PULLUP)
static bool     btnLastRead    = true;
static uint32_t btnLastEdgeMs  = 0;
static uint32_t btnHeldSinceMs = 0;
static bool     btnPressEvent  = false;    // one-shot, consumed by state logic

/* pacing */
static uint32_t nextSampleUs   = 0;
static uint32_t nextTelemMs    = 0;   // serial "T," lines (calibration)
static uint32_t nextStatusMs   = 0;   // off-board status for the dashboard
static uint32_t nextBeepMs     = 0;
static bool     beepOn         = false;

#if USE_OLED
static uint32_t nextUiMs       = 0;
#endif

#if USE_PROXIMITY
static uint32_t nextProxMs     = 0;
static bool     obstacleNear   = false;
#endif

/* ------------------------------------------------------------------ */
/*  Buzzer — handles both ESP32 Arduino core 2.x and 3.x LEDC APIs     */
/* ------------------------------------------------------------------ */
static void buzzerInit() {
#if PIN_BUZZER >= 0
  #if defined(ESP_ARDUINO_VERSION_MAJOR) && (ESP_ARDUINO_VERSION_MAJOR >= 3)
    ledcAttach(PIN_BUZZER, 2000, 10);
    ledcWrite(PIN_BUZZER, 0);
  #else
    ledcSetup(0, 2000, 10);
    ledcAttachPin(PIN_BUZZER, 0);
    ledcWrite(0, 0);
  #endif
#endif
}

static void buzzerTone(uint32_t freqHz) {   // 0 = off
#if PIN_BUZZER >= 0
  #if defined(ESP_ARDUINO_VERSION_MAJOR) && (ESP_ARDUINO_VERSION_MAJOR >= 3)
    if (freqHz) ledcWriteTone(PIN_BUZZER, freqHz);
    else        ledcWrite(PIN_BUZZER, 0);
  #else
    if (freqHz) ledcWriteTone(0, freqHz);
    else        ledcWrite(0, 0);
  #endif
#else
  (void)freqHz;
#endif
}

/* ------------------------------------------------------------------ */
/*  Status JSON — what the phone dashboard renders                     */
/* ------------------------------------------------------------------ */
static int buildStatusJson(char *buf, size_t n) {
  int cd = 0;
  if (state == ST_COUNTDOWN) {
    int32_t leftMs = (int32_t)(countdownEndMs - millis());
    cd = (leftMs > 0) ? (int)((leftMs + 999) / 1000) : 0;
  }
  return snprintf(buf, n,
      "{\"g\":%.2f,\"pk\":%.2f,\"st\":%u,\"cd\":%d,\"up\":%lu}",
      liveG, peakG, (unsigned)state, cd, (unsigned long)millis());
}

/* ------------------------------------------------------------------ */
/*  BLE — GATT server: crash alerts out, live status out, commands in  */
/* ------------------------------------------------------------------ */
#if USE_BLE
static BLECharacteristic *evtChar          = nullptr;
static BLECharacteristic *tlmChar          = nullptr;
static BLEServer         *bleServer        = nullptr;
static volatile bool      bleLinked        = false;
static volatile uint16_t  bleConnId        = 0;
static volatile uint32_t  lastLinkActivityMs = 0;   // last heartbeat/command
static uint32_t           readvertiseAtMs  = 0;     // 0 = nothing pending
static uint8_t            alertNotifyCount = 0;
static uint32_t           nextNotifyMs     = 0;

class CrashGuardServerCB : public BLEServerCallbacks {
  void onConnect(BLEServer *s, esp_ble_gatts_cb_param_t *param) override {
    bleLinked = true;
    bleConnId = param->connect.conn_id;
    lastLinkActivityMs = millis();
    Serial.println("MSG,BLE client connected");
  }
  void onDisconnect(BLEServer *s) override {
    bleLinked = false;
    /* Do NOT call startAdvertising() here — the ESP32 BLE stack is not ready
     * at this instant and the call silently fails, leaving the board
     * undiscoverable until a power cycle. Flag it and restart advertising
     * from loop() after a short settle delay instead. */
    readvertiseAtMs = millis() + 500;
    Serial.println("MSG,BLE client disconnected — re-advertising shortly");
  }
};

/* phone -> server -> bridge -> this write */
class CmdCB : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    String v = c->getValue().c_str();
    lastLinkActivityMs = millis();          // any write proves the bridge lives
    if      (v == "CANCEL") remoteCmd = CMD_CANCEL;
    else if (v == "REARM")  remoteCmd = CMD_REARM;
    /* "PING" is the bridge's heartbeat — it only refreshes the activity
     * timestamp above. Without it we cannot tell a killed bridge from an idle
     * one, because an abruptly terminated program never sends a BLE disconnect. */
  }
};

static void bleInit() {
  BLEDevice::init(BLE_DEVICE_NAME);
  BLEServer *server = BLEDevice::createServer();
  bleServer = server;
  server->setCallbacks(new CrashGuardServerCB());

  BLEService *svc = server->createService(BLE_SVC_UUID);

  evtChar = svc->createCharacteristic(
      BLE_EVT_UUID,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  evtChar->addDescriptor(new BLE2902());
  evtChar->setValue("{\"evt\":\"boot\"}");

  tlmChar = svc->createCharacteristic(
      BLE_TLM_UUID,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  tlmChar->addDescriptor(new BLE2902());

  BLECharacteristic *cmdChar = svc->createCharacteristic(
      BLE_CMD_UUID, BLECharacteristic::PROPERTY_WRITE);
  cmdChar->setCallbacks(new CmdCB());

  svc->start();

  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(BLE_SVC_UUID);
  adv->setScanResponse(true);
  BLEDevice::startAdvertising();
  Serial.println("MSG,BLE advertising as " BLE_DEVICE_NAME);
}

static void bleSendAlert(const char *json) {
  if (!evtChar) return;
  evtChar->setValue((uint8_t *)json, strlen(json));
  evtChar->notify();
}

static void bleSendStatus() {
  if (!tlmChar || !bleLinked) return;
  static char json[112];
  int n = buildStatusJson(json, sizeof(json));
  tlmChar->setValue((uint8_t *)json, (size_t)n);
  tlmChar->notify();
}

/* Keeps the board reconnectable without a power cycle. Two jobs:
 *   1. Restart advertising a moment AFTER a disconnect (doing it inside the
 *      disconnect callback fails silently on the ESP32).
 *   2. Drop a dead link: if the bridge was killed (Ctrl+C / IDE stop) it never
 *      sends a BLE disconnect, so the board would sit "connected" forever and
 *      refuse new clients. Missing heartbeats force the link down. */
static void bleMaintain() {
  uint32_t now = millis();

  if (readvertiseAtMs && (int32_t)(now - readvertiseAtMs) >= 0) {
    readvertiseAtMs = 0;
    BLEDevice::startAdvertising();
    Serial.println("MSG,BLE advertising again — bridge may reconnect");
  }

  if (bleLinked && lastLinkActivityMs &&
      (now - lastLinkActivityMs) > BLE_LINK_TIMEOUT_MS) {
    Serial.println("ERR,BLE bridge went silent — dropping stale link");
    if (bleServer) bleServer->disconnect(bleConnId);
    bleLinked = false;
    lastLinkActivityMs = 0;
    readvertiseAtMs = now + 500;
  }
}
#endif /* USE_BLE */

/* ------------------------------------------------------------------ */
/*  Wi-Fi mode: board talks straight to the server on the hotspot.     */
/*  Each status POST's *response* carries any pending phone command,   */
/*  so cancel latency is bounded by 1/STATUS_HZ.                       */
/* ------------------------------------------------------------------ */
#if USE_WIFI_HTTP
static void wifiInit() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("MSG,WiFi connecting to " WIFI_SSID " ");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("MSG,WiFi connected, IP=");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("ERR,WiFi connect FAILED — alerts and dashboard will not work");
  }
}

static bool wifiPost(const char *path, const char *json, String *respOut) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.setConnectTimeout(800);
  http.begin(String(SERVER_BASE_URL) + path);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST((uint8_t *)json, strlen(json));
  if (respOut && code > 0) *respOut = http.getString();
  http.end();
  return code >= 200 && code < 300;
}

static void wifiSendStatus() {
  static char json[112];
  buildStatusJson(json, sizeof(json));
  String resp;
  if (wifiPost("/telemetry", json, &resp)) {
    if      (resp.indexOf("CANCEL") >= 0) remoteCmd = CMD_CANCEL;
    else if (resp.indexOf("REARM")  >= 0) remoteCmd = CMD_REARM;
  }
}
#endif /* USE_WIFI_HTTP */

/* ------------------------------------------------------------------ */
/*  Sensor helpers                                                     */
/* ------------------------------------------------------------------ */

/* Read all three ACCELEROMETER axes in TRUE g.
 *
 * Performs a single 6-byte I2C burst so X, Y and Z are sampled at the SAME
 * instant, deliberately instead of getAccelX/Y/Z(). Verified against the
 * official MYOSA library source, those per-axis getters:
 *   1. each re-read the range register over I2C (3x the bus traffic at 100 Hz,
 *      which returns intermittent garbage and caused erratic readings),
 *   2. sample the axes at different moments, corrupting the vector magnitude,
 *   3. apply a scale factor that is 2x too large for every range
 *      (AccelAndGyro.cpp line 41 uses 2^(fsr+1) where it should use 2^fsr).
 * The library's own burst accessor getAccel() is declared private, so we read
 * the registers directly; the library is still used for begin() and range.
 *
 * We take the raw int16 counts and apply the correct MPU6050 sensitivity
 * ourselves. NO gyroscope function is called anywhere in this firmware —
 * rotation rate is not needed for crash detection. */
static bool readAccelG(float *gx, float *gy, float *gz, bool *railed) {
  /* Burst-read the six accelerometer bytes ourselves. The library's own
   * burst accessor is private, and its public per-axis getters have the
   * problems described above, so we talk to the MPU6050 directly. The
   * library is still used for begin() and setFullScaleAccelRange(). */
  Wire.beginTransmission(MPU6050_ADDRESS_AD0_HIGH);
  Wire.write(MPU6050_ACCEL_XOUT_H_REG);
  if (Wire.endTransmission(false) != 0) return false;   // repeated start
  if (Wire.requestFrom((uint8_t)MPU6050_ADDRESS_AD0_HIGH, (uint8_t)6) != 6) {
    return false;
  }
  uint8_t b[6];
  for (uint8_t i = 0; i < 6; i++) b[i] = Wire.read();

  /* Read into named locals first: the order of evaluation of Wire.read()
   * calls inside one expression is unspecified in C++. */
  int16_t rx = (int16_t)(((uint16_t)b[0] << 8) | b[1]);
  int16_t ry = (int16_t)(((uint16_t)b[2] << 8) | b[3]);
  int16_t rz = (int16_t)(((uint16_t)b[4] << 8) | b[5]);

  *gx = (float)rx / ACCEL_LSB_PER_G;
  *gy = (float)ry / ACCEL_LSB_PER_G;
  *gz = (float)rz / ACCEL_LSB_PER_G;
  /* int16 rails at +/-32767; treat near-full-scale as saturated */
  *railed = (abs(rx) >= 32000) || (abs(ry) >= 32000) || (abs(rz) >= 32000);
  return true;
}

/* Average |a| while the board sits still -> "this is what 1 g reads as".
 * Normalizing against this makes CRASH_G_THRESHOLD immune to any library
 * scale-factor surprises, and self-corrects for mounting orientation. */
static void measureBaseline() {
#if USE_OLED
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(WHITE);
  display.setCursor(0, 0);
  display.print("Calibrating...");
  display.setCursor(0, 16);
  display.print("Keep the car STILL");
  display.display();
#endif
  Serial.println("MSG,calibrating baseline — keep the car STILL");

  float sx = 0, sy = 0, sz = 0;
  float minV = 1e9f, maxV = -1e9f;
  uint16_t good = 0;
  for (uint16_t i = 0; i < BASELINE_SAMPLES; i++) {
    float gx, gy, gz; bool railed;
    if (readAccelG(&gx, &gy, &gz, &railed)) {
      sx += gx; sy += gy; sz += gz;
      float m = sqrtf(gx * gx + gy * gy + gz * gz);
      if (m < minV) minV = m;
      if (m > maxV) maxV = m;
      good++;
    }
    delay(12);
  }
  if (good == 0) {
    Serial.println("ERR,no accelerometer data during calibration!");
    gravX = 0; gravY = 0; gravZ = 1.0f; baselineMag = 1.0f;
    return;
  }
  /* Gravity VECTOR in g. Subtracting it gives DYNAMIC acceleration: 0.00 g
   * when still in ANY orientation, and true impact g during a crash. */
  gravX = sx / good; gravY = sy / good; gravZ = sz / good;
  baselineMag = sqrtf(gravX * gravX + gravY * gravY + gravZ * gravZ);

  float spread = (maxV - minV);
  if (spread > 0.15f) {
    Serial.print("ERR,baseline measured while MOVING (spread=");
    Serial.print(spread, 2);
    Serial.println(" g). Power-cycle and keep the board STILL during boot!");
    for (uint8_t i = 0; i < 5; i++) {
      buzzerTone(2600); delay(80); buzzerTone(0); delay(80);
    }
  }
  if (baselineMag < 0.80f || baselineMag > 1.25f) {
    Serial.print("ERR,gravity magnitude reads ");
    Serial.print(baselineMag, 2);
    Serial.println(" g but should be ~1.00 — check ACCEL_LSB_PER_G vs the range.");
  }

  Serial.print("MSG,gravity=(");
  Serial.print(gravX, 3); Serial.print(",");
  Serial.print(gravY, 3); Serial.print(",");
  Serial.print(gravZ, 3);
  Serial.print(") |g|="); Serial.print(baselineMag, 3);
  Serial.print("  spread="); Serial.print(spread, 3);
  Serial.println(" g   (dynamic G reads 0.00 at rest, in ANY orientation)");
}

/* ------------------------------------------------------------------ */
/*  Alert delivery                                                     */
/* ------------------------------------------------------------------ */
static void sendCrashAlert() {
  static char json[128];
  snprintf(json, sizeof(json),
           "{\"evt\":\"crash\",\"peak_g\":%.2f,\"decel_g\":%.2f,\"axis\":\"%c\","
           "\"saturated\":%d,\"uptime_ms\":%lu}",
           crashPeakG, crashDecelG, crashAxis, crashSaturated ? 1 : 0,
           (unsigned long)millis());
  Serial.print("EVT,ALERT,");
  Serial.println(json);

#if USE_BLE
  bleSendAlert(json);
  alertNotifyCount = 1;
  nextNotifyMs = millis() + 3000;   // re-notify a few times for late bridges
#endif
#if USE_WIFI_HTTP
  wifiPost("/crash-alert", json, nullptr);
#endif
}

/* ------------------------------------------------------------------ */
/*  OLED screens — bench debugging only (USE_OLED in config.h).        */
/*  In the demo the car is moving; the phone dashboard is the UI.      */
/* ------------------------------------------------------------------ */
#if USE_OLED
static void drawMonitoring() {
  display.clearDisplay();
  display.setTextColor(WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("CrashGuard  ARMED");
#if USE_BLE
  display.setCursor(98, 0);
  display.print(bleLinked ? "BLE*" : "BLE?");
#endif
  display.setCursor(0, 16);
  display.print("Impact");
  display.setTextSize(3);
  display.setCursor(0, 28);
  display.print(liveG, 2);
  display.setTextSize(1);
  display.setCursor(78, 40);
  display.print("G");
  display.setCursor(0, 56);
  display.print("peak ");
  display.print(peakG, 2);
  display.print(" G");
  display.display();
}

static void drawCountdown(uint8_t secsLeft) {
  display.clearDisplay();
  display.setTextColor(WHITE);
  display.setTextSize(1);
  display.setCursor(4, 0);
  display.print("!! CRASH DETECTED !!");
  display.setTextSize(4);
  display.setCursor((secsLeft >= 10) ? 40 : 52, 18);
  display.print(secsLeft);
  display.setTextSize(1);
  display.setCursor(0, 56);
  display.print("CANCEL: phone or btn");
  display.display();
}

static void drawAlertSent() {
  display.clearDisplay();
  display.setTextColor(WHITE);
  display.setTextSize(2);
  display.setCursor(8, 0);
  display.print("EMERGENCY");
  display.setTextSize(1);
  display.setCursor(0, 24);
  display.print("Alert sent to host.");
  display.setCursor(0, 36);
  display.print("AI calling contact...");
  display.setCursor(0, 56);
  display.print("peak ");
  display.print(crashPeakG, 2);
  display.print(" G");
  display.display();
}
#endif /* USE_OLED */

/* ------------------------------------------------------------------ */
/*  Button handling (debounced press + long-press) — onboard backup    */
/* ------------------------------------------------------------------ */
static void pollButton() {
  bool raw = digitalRead(PIN_BUTTON);        // HIGH = released (pull-up)
  uint32_t now = millis();

  if (raw != btnLastRead) {                  // edge seen — start debounce timer
    btnLastRead = raw;
    btnLastEdgeMs = now;
  }
  if ((now - btnLastEdgeMs) > 30 && raw != btnStable) {
    btnStable = raw;
    if (btnStable == LOW) {                  // clean press
      btnPressEvent = true;
      btnHeldSinceMs = now;
    }
  }
}

static bool longPressActive() {
  return (btnStable == LOW) && (millis() - btnHeldSinceMs >= LONGPRESS_MS);
}

/* ------------------------------------------------------------------ */
/*  Shared state transitions (phone command OR physical button)        */
/* ------------------------------------------------------------------ */
static void doCancel(const char *who) {
  state = ST_DISARMED;
  disarmSplashMs = millis() + 1500;
  peakG = 0.0f;
  buzzerTone(0);
  Serial.print("EVT,DISARMED_BY_");
  Serial.println(who);
}

static void doRearm() {
  state = ST_MONITORING;
  peakG = 0.0f;
  overCount = 0;
#if USE_BLE
  alertNotifyCount = 0;
#endif
  Serial.println("EVT,REARMED");
  buzzerTone(1200); delay(60); buzzerTone(0);
}

/* ------------------------------------------------------------------ */
/*  Setup                                                              */
/* ------------------------------------------------------------------ */
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("MSG,CrashGuard boot");

  pinMode(PIN_BUTTON, INPUT_PULLUP);
  buzzerInit();

  Wire.begin();
  Wire.setClock(I2C_CLOCK_HZ);

#if USE_OLED
  if (!display.begin()) {
    Serial.println("ERR,OLED not found at 0x3C — check the JST chain");
  }
#endif

  /* Accel/gyro — retry until the module answers, like the MYOSA examples */
  while (!Ag.begin()) {
    Serial.println("ERR,AccelAndGyro (0x69) not found — check connections");
    delay(1000);
  }
  /* +/-16 g range: crash spikes clip badly at the +/-2 g default */
  Ag.setFullScaleAccelRange(MPU_ACCEL_CONFIG_FS_SEL_16g);
  /* Confirm the range register really took — ACCEL_LSB_PER_G depends on it. */
  {
    uint8_t fsr = Ag.getFullScaleAccelRange();
    Serial.print("MSG,accel range fsr_sel=");
    Serial.print(fsr);
    Serial.println(fsr == MPU_ACCEL_CONFIG_FS_SEL_16g
                   ? " (+/-16 g, ACCEL_LSB_PER_G must be 2048)"
                   : " UNEXPECTED — update ACCEL_LSB_PER_G to match!");
  }

#if USE_PROXIMITY
  if (Lpg.begin()) {
    Lpg.enableProximitySensor();
    Serial.println("MSG,proximity sensor enabled");
  } else {
    Serial.println("ERR,APDS9960 not found — proximity warnings disabled");
  }
#endif

  measureBaseline();

#if USE_BLE
  bleInit();
#endif
#if USE_WIFI_HTTP
  wifiInit();
#endif

  /* short ready chirp */
  buzzerTone(1500); delay(80); buzzerTone(0);

  nextSampleUs = micros();
  Serial.println("MSG,armed — open the phone dashboard for live status");
  Serial.println("MSG,telemetry format: T,<millis>,<G>,<peakG>,<state>");
}

/* ------------------------------------------------------------------ */
/*  Main loop                                                          */
/* ------------------------------------------------------------------ */
void loop() {
  uint32_t nowMs = millis();
  pollButton();

  /* ---------- paced accelerometer sampling ---------- */
  if ((int32_t)(micros() - nextSampleUs) >= 0) {
    nextSampleUs += 1000000UL / SAMPLE_HZ;

    float gx, gy, gz; bool railed = false;
    if (readAccelG(&gx, &gy, &gz, &railed)) {

      /* DYNAMIC acceleration = measured - gravity, in true g.
       * Reads 0.00 at rest in ANY orientation, because the gravity vector
       * (not just its magnitude) is removed. */
      float dx = gx - gravX;
      float dy = gy - gravY;
      float dz = gz - gravZ;
      float raw = sqrtf(dx * dx + dy * dy + dz * dz);

      /* Median-of-3 spike rejection. */
      gHist[gHistIdx] = raw;
      gHistIdx = (gHistIdx + 1) % 3;
      float a1 = gHist[0], b1 = gHist[1], c1 = gHist[2];
      float med = (a1 > b1) ? ((b1 > c1) ? b1 : ((a1 > c1) ? c1 : a1))
                            : ((a1 > c1) ? a1 : ((b1 > c1) ? c1 : b1));
      liveG = med;
      if (liveG > peakG) peakG = liveG;

      if (state == ST_MONITORING && nowMs > ARM_DELAY_MS) {
        if (railed) satCount++; else satCount = 0;
        bool saturatedHit = (satCount >= IMPACT_MIN_SAMPLES);

        if (liveG >= CRASH_G_THRESHOLD || saturatedHit) {
          if (++overCount >= IMPACT_MIN_SAMPLES || saturatedHit) {
            /* record WHICH axis and WHICH direction */
            float mx = fabsf(dx), my = fabsf(dy), mz = fabsf(dz);
            float dom;
            if (mx >= my && mx >= mz)      { crashAxis = 'X'; dom = dx; }
            else if (my >= mx && my >= mz) { crashAxis = 'Y'; dom = dy; }
            else                           { crashAxis = 'Z'; dom = dz; }
            crashDecelG    = dom;          /* negative = deceleration */
            crashSaturated = railed;
            crashPeakG     = peakG;

            state = ST_COUNTDOWN;
            countdownEndMs = nowMs + COUNTDOWN_SECONDS * 1000UL;
            overCount = 0; satCount = 0;

            Serial.print("EVT,IMPACT,peak_g=");
            Serial.print(crashPeakG, 2);
            Serial.print(",axis="); Serial.print(crashAxis);
            Serial.print(",decel_g="); Serial.print(crashDecelG, 2);
            Serial.print(",saturated="); Serial.println(crashSaturated ? 1 : 0);
          }
        } else {
          overCount = 0;
        }
      }
    }
  }

  /* ---------- consume one remote command per pass ---------- */
  RemoteCmd cmd = remoteCmd;
  remoteCmd = CMD_NONE;

  /* ---------- state behavior ---------- */
  switch (state) {

    case ST_MONITORING:
#if USE_OLED
      if (nowMs >= nextUiMs) { nextUiMs = nowMs + 150; drawMonitoring(); }
#endif
      break;

    case ST_COUNTDOWN: {
      int32_t leftMs = (int32_t)(countdownEndMs - nowMs);

      if (cmd == CMD_CANCEL) {                   /* phone CANCEL button */
        doCancel("PHONE");
        break;
      }
      if (btnPressEvent) {                       /* onboard backup button */
        btnPressEvent = false;
        doCancel("BUTTON");
        break;
      }

      if (leftMs <= 0) {                         /* nobody canceled -> alert */
        state = ST_ALERT_SENT;
        buzzerTone(0);
        sendCrashAlert();
#if USE_OLED
        drawAlertSent();
#endif
        /* urgent triple beep */
        for (uint8_t i = 0; i < 3; i++) {
          buzzerTone(2400); delay(120); buzzerTone(0); delay(80);
        }
        break;
      }

      /* alarm beeping — urgency ramps in the last 3 seconds */
      {
        uint32_t period = (leftMs < 3000) ? 250 : 600;
        if (nowMs >= nextBeepMs) {
          nextBeepMs = nowMs + period / 2;
          beepOn = !beepOn;
          buzzerTone(beepOn ? 2000 : 0);
        }
      }
#if USE_OLED
      if (nowMs >= nextUiMs) {
        nextUiMs = nowMs + 120;
        drawCountdown((uint8_t)((leftMs + 999) / 1000));
      }
#endif
      break;
    }

    case ST_ALERT_SENT:
#if USE_BLE
      /* Re-notify a few times so a bridge that reconnects late still hears it */
      if (alertNotifyCount > 0 && alertNotifyCount < 5 && nowMs >= nextNotifyMs) {
        static char json[128];
        snprintf(json, sizeof(json),
                 "{\"evt\":\"crash\",\"peak_g\":%.2f,\"decel_g\":%.2f,\"axis\":\"%c\","
                 "\"saturated\":%d,\"uptime_ms\":%lu}",
                 crashPeakG, crashDecelG, crashAxis, crashSaturated ? 1 : 0,
                 (unsigned long)millis());
        bleSendAlert(json);
        alertNotifyCount++;
        nextNotifyMs = nowMs + 3000;
      }
#endif
      if (btnPressEvent) btnPressEvent = false;  /* short press ignored here */
      if (cmd == CMD_REARM || longPressActive()) {
        doRearm();                               /* phone REARM or long-press */
      }
#if USE_OLED
      if (nowMs >= nextUiMs) { nextUiMs = nowMs + 500; drawAlertSent(); }
#endif
      break;

    case ST_DISARMED:
      if (nowMs >= disarmSplashMs) {
        state = ST_MONITORING;
        overCount = 0;
        Serial.println("EVT,REARMED");
      }
      break;
  }

#if USE_PROXIMITY
  /* ---------- optional forward-obstacle warning ---------- */
  if (nowMs >= nextProxMs) {
    nextProxMs = nowMs + 1000 / PROX_POLL_HZ;
    float p = Lpg.getProximity(false);
    bool nearNow = (p > 0.0f) && (p <= PROX_WARN_THRESHOLD);
    if (nearNow && !obstacleNear && state == ST_MONITORING) {
      buzzerTone(1000); delay(40); buzzerTone(0);   /* short warning blip */
      Serial.print("EVT,OBSTACLE,prox=");
      Serial.println(p, 2);
    }
    obstacleNear = nearNow;
  }
#endif

#if USE_BLE
  bleMaintain();          /* re-advertise after disconnect; drop dead links */
#endif

  /* ---------- live status for the PHONE DASHBOARD ---------- */
  if (nowMs >= nextStatusMs) {
    nextStatusMs = nowMs + 1000 / STATUS_HZ;
#if USE_BLE
    bleSendStatus();
#endif
#if USE_WIFI_HTTP
    wifiSendStatus();      /* response also carries phone CANCEL/REARM */
#endif
  }

  /* ---------- serial telemetry for calibration & logging ---------- */
  if (nowMs >= nextTelemMs) {
    nextTelemMs = nowMs + 1000 / TELEMETRY_HZ;
    Serial.print("T,");
    Serial.print(nowMs);
    Serial.print(',');
    Serial.print(liveG, 3);
    Serial.print(',');
    Serial.print(peakG, 3);
    Serial.print(',');
    Serial.println((int)state);
  }
}
