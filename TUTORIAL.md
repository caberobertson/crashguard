# CrashGuard — Build, Test & Demo Tutorial

**Team:** Aidan Garcia · Jacob Randall · Cabe Robertson · Vu Ha Nguyen — NMSU, MYOSA 6.0 (2026)

This is the complete walkthrough: what every file does, how to bring the system up in
stages, how to test each stage **before** you need the next one, how to calibrate on the
RC car, and the exact runbook for the conference demo.

Golden rule used throughout: **never test two new things at once.** Each stage below
proves one layer, offline where possible, before the next layer is added.

---

## 0. What you're building

```
 IMPACT ──► MYOSA board ──► 10 s countdown ──► (nobody cancels) ──► BLE alert
                                  │
                            button press = false alarm, re-arm
                                  
 BLE alert ──► bridge_ble.py ──► call_server.py ──► Twilio dials phone
        (also live status ▲ / phone cmds ▼)   │
                                       │      └► Claude agent answers the
                                       ▼          dispatcher's questions from
                              PHONE DASHBOARD     victim_profile.json
                         http://<laptop>:5000/
                     live G · countdown · CANCEL
```

The car is moving, so **all results show on a phone**, not on the board: the
dashboard is a mobile web page served by the call server. The countdown's CANCEL
button lives there (phone vibrates too); the onboard button is just a wired backup.

Think OnStar: crash detected → automated call → vital info spoken → questions answered.
For the demo, "emergency services" is the presenter's phone. **Never point this at 911.**

---

## 1. Bill of materials

| Item | Notes |
|---|---|
| MYOSA Mini kit | Motherboard (ESP32) + MPU6050 accel module are all you need. OLED optional (bench debugging only — `USE_OLED` in config.h). APDS9960 optional. |
| RC car | Any cheap one. The board just rides on top. |
| USB power bank | Small/light; powers the motherboard on the car. |
| Passive piezo buzzer | GPIO25 → buzzer → GND. Skip it by setting `PIN_BUZZER -1`. |
| Momentary pushbutton | GPIO0 → button → GND (bench: the ESP32 **BOOT** button already works). |
| Zip ties / velcro / foam | Mounting + vibration padding. 3D-printed case later if time allows. |
| Laptop with Bluetooth | Runs the bridge + call server (which serves the phone dashboard). |
| A phone for the dashboard | Any phone with a browser, on the same hotspot as the laptop. Can be the presenter's. |
| Phone hotspot | Internet for Twilio/Claude at the venue. Don't trust venue Wi-Fi. |

Accounts (all free/cheap): **Twilio** trial (~$15 credit; a number costs ~$1/mo),
**Anthropic API** key, **ngrok** free tier.

---

## 2. One-time software setup

### 2.1 Arduino side
1. Install **Arduino IDE 2.x**.
2. Install the ESP32 core: *File → Preferences → Additional boards manager URLs* →
   `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
   then *Boards Manager* → install **esp32 by Espressif**.
3. Install the **official MYOSA libraries**:
   ```bash
   git clone https://github.com/myosa-sensors/arduino-libraries
   ```
   Copy `AccelAndGyro`, `OLED`, `LightProximityAndGesture` (and `MYOSA` if you want the
   official examples) into your `Documents/Arduino/libraries/` folder. Restart the IDE.
4. Board selection: **ESP32 Dev Module** (or the exact MYOSA board entry if listed),
   correct COM port, 115200 baud monitor.

### 2.2 Python side
```bash
cd host
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```
Windows note for BLE: `bleak` uses WinRT — works on Win10/11 with Bluetooth on.

---

## 3. Stage 0 — bench bring-up (prove the hardware)

Chain the modules to the motherboard with the JST cables, plug in USB.

**I2C scan** — paste this micro-sketch, upload, open Serial Monitor @115200:
```cpp
#include <Wire.h>
void setup(){ Serial.begin(115200); Wire.begin(); Wire.setClock(100000); }
void loop(){
  Serial.println("scan:");
  for(uint8_t a=1;a<127;a++){ Wire.beginTransmission(a);
    if(Wire.endTransmission()==0){ Serial.print(" found 0x"); Serial.println(a,HEX);} }
  delay(3000);
}
```
Expect **0x69** (accel) — that's the only required module. (The MYOSA accel module
ties AD0 high, hence 0x69 — the official library already defaults to it.) If you've
chained the optional OLED you'll also see **0x3C**. Missing an address → reseat that
JST cable.

Optionally run the official example `5_AccelAndGyro_Demo` from the MYOSA library to
sanity-check the module.

---

## 4. Stage 1 — flash CrashGuard

Open `firmware/crashguard/crashguard.ino`. Skim `config.h` — everything tunable is
there:

| Setting | Meaning | Start with |
|---|---|---|
| `USE_BLE` / `USE_WIFI_HTTP` | alert transport | BLE=1, WiFi=0 |
| `CRASH_G_THRESHOLD` | normalized g's that count as a crash | 3.0 (until Stage 6) |
| `IMPACT_MIN_SAMPLES` | consecutive samples over threshold | 1 |
| `COUNTDOWN_SECONDS` | occupant cancel window | 10 |
| `ARM_DELAY_MS` | ignore impacts right after boot | 4000 |
| `PIN_BUTTON` / `PIN_BUZZER` | wiring | 0 / 25 |

Upload. **Keep the board still during boot** — it spends ~1.5 s measuring its 1 g
baseline (serial prints "calibrating baseline — keep the car STILL"). Serial shows:

```
MSG,CrashGuard boot
MSG,baseline_cm_s2=981.3        ← whatever *your* board reads as 1 g
MSG,BLE advertising as MYOSA-CrashGuard
MSG,armed
T,5210,1.002,1.014,0            ← telemetry: millis, G, peakG, state
```

Why the baseline matters: the firmware never trusts absolute sensor units. It measures
what "sitting still" reads as, then reports everything as a multiple of that. A crash
threshold of 3.0 therefore means "3× gravity" on *your* board, regardless of any
library scale factors or mounting angle.

**Bench crash test (serial-only for now — the pretty version comes in Stage 3):**
smack the board (or the table) sharply.
- Buzzer starts beeping (faster in the last 3 s); serial: `EVT,IMPACT,peak_g=…`.
- Press BOOT within 10 s → serial `EVT,DISARMED_BY_BUTTON`, back to monitoring.
- Smack again, let it expire → serial `EVT,ALERT,{"evt":"crash","peak_g":4.87,…}`.
  Hold BOOT 1.5 s to re-arm (`EVT,REARMED`).

If it triggers while just picking the board up, raise `CRASH_G_THRESHOLD` — real
numbers come in Stage 6.

---

## 5. Stage 2 — host software, fully offline (no accounts, no cost)

```bash
cd host
python test_offline.py
```
This drives the **real Flask app in-process** through the exact webhook sequence Twilio
will use: trigger → cooldown dedupe → `/voice` briefing → three dispatcher Q&As →
hangup → silence handling. You should see **25 PASS** — including the phone-dashboard pipeline (status in,
commands out both ways).

Rehearse the conversation itself:
```bash
python console_dispatcher_test.py --mock
```
You type the dispatcher's lines; CrashGuard answers. Edit `victim_profile.json` (this
is the "preloaded information" from the proposal — name, blood type, GPS, contacts,
medical info) and rehearse again until the briefing sounds right.

Now add the real AI (first paid piece, pennies per test):
```bash
cp .env.example .env        # put your ANTHROPIC_API_KEY in .env
python console_dispatcher_test.py
```
Ask it curveballs — "is the driver conscious?", "what's the cross street?", "spell the
name" — and confirm it stays grounded and admits what it doesn't know.

---

## 6. Stage 2.5 — the phone dashboard (your new UI)

Start the server and the bridge, power the board:

```bash
python call_server.py     # prints: PHONE DASHBOARD -> http://192.168.x.x:5000/
python bridge_ble.py      # relays board status up + phone commands down
```

On a phone **on the same network as the laptop**, open that URL. You'll see:

- green **board live** dot, big live G number, session peak, occupant name;
- smack the board → **!! CRASH DETECTED !!**, full-screen countdown, phone vibrates,
  giant red **CANCEL — I'M OK** button → tap it → board disarms
  (serial: `EVT,DISARMED_BY_PHONE`);
- let a countdown expire → banner **EMERGENCY — CALL IN PROGRESS** and a blue
  **RE-ARM SYSTEM** button.

No board handy yet? Fake it: `curl -X POST localhost:5000/telemetry -H "Content-Type: application/json" -d '{"g":5.1,"pk":5.1,"st":1,"cd":7}'` — the dashboard reacts instantly.

**Live GPS — automatic.** The dashboard streams the phone's real GPS to the server
continuously in the background (no button — a crash victim couldn't press one). The
next crash call reads out the phone's *actual* coordinates, with a street address
(best-effort reverse geocode), instead of the preloaded `victim_profile.json`
location. If permission is denied or GPS is unavailable, it quietly falls back to the
preloaded value.

The **only** interaction is a one-time browser permission prompt ("Allow location?")
the first time the dashboard opens — that's an OS privacy safeguard that can't be
skipped. Tap Allow once when you set up, and it's hands-off from then on. Do this
during setup, before the demo starts, so there's nothing to touch during the run.

> **Must be HTTPS.** Phone browsers only give up GPS over a secure page. The
> `http://<laptop-ip>:5000/` URL will NOT work for the location button. Open the
> dashboard on your phone via your **ngrok HTTPS URL** instead
> (`https://<sub>.ngrok-free.app/`) — the same tunnel you run for Twilio serves the
> dashboard too. On that URL, the location button works.

Cancel latency budget: board streams status at 5 Hz and the bridge polls commands
every 250 ms, so a phone tap reaches the board in well under a second — trivial
inside the 10-second window.

## 7. Stage 3 — first real phone call

1. **Twilio**: create account → verify the presenter's personal number (trial accounts
   can only call verified numbers — that's fine) → get a Twilio number → copy Account
   SID, Auth Token, and the number into `.env`.
   `EMERGENCY_CONTACT_NUMBER` = the presenter's phone. **Never 911. Ever.**
2. **ngrok** (Twilio must reach your laptop): `ngrok http 5000` → paste the
   `https://….ngrok-free.app` URL into `PUBLIC_BASE_URL` in `.env`.
   (ngrok restarts = new URL = update `.env` = restart server. #1 gotcha.)
3. Run it:
   ```bash
   python call_server.py            # terminal 1
   python simulate_crash.py --peak-g 5.2   # terminal 2
   ```
   Presenter's phone rings → briefing plays → ask it questions out loud → say
   "no further questions, goodbye" → clean hangup. Trial accounts play a short
   "trial account" preamble first; that's normal (upgrading removes it).

Useful knobs in `.env`: `DRY_RUN=1` (log triggers, don't dial — great for rehearsal),
`MOCK_AI=1` (call flow without Claude), `TTS_VOICE`, `TRIGGER_COOLDOWN_S`.

---

## 8. Stage 4 — close the Bluetooth loop

```bash
python call_server.py    # terminal 1
python bridge_ble.py     # terminal 2 → "connected — relaying status…"
```
Dashboard open on the phone, board on the bench: smack it, don't cancel. Watch the
chain fire: countdown on the dashboard → `EVT,ALERT` (serial) →
`[bridge] CRASH received … POST /trigger-call` → **the phone rings while the
dashboard shows EMERGENCY — CALL IN PROGRESS.**

That's the entire proposal pipeline working end-to-end on a desk.

Flaky BLE at the venue? Flip the board to Wi-Fi mode: in `config.h` set `USE_BLE 0`,
`USE_WIFI_HTTP 1`, fill in the hotspot SSID/password and `SERVER_BASE_URL` with the
laptop's hotspot IP (`ipconfig`/`ifconfig`) — the board then talks straight to the
server, no bridge needed. The dashboard (and its CANCEL/REARM buttons) work
identically: each status POST's response carries any pending phone command.

---

## 9. Stage 5 — RC car integration

**Mounting:** power bank low/flat on the chassis, motherboard on 3–5 mm of foam tape
(padding stops motor vibration from looking like mini-impacts), modules zip-tied so JST
cables can't flex loose, button reachable, buzzer exposed. Short USB cable, strain-
relieved. A 3D-printed case is a nice upgrade; measure the stack and design around the
JST chain.

**Power-on ritual (matters!):** place the car on the ground → power on → hold still
through "Calibrating…" → drive only after `MSG,armed`.

---

## 10. Stage 6 — calibrate the threshold with data

The firmware streams `T,millis,G,peakG,state` at 20 Hz. Close the Arduino Serial
Monitor first (only one program can own the port), find your port (IDE → Tools → Port),
then:

```bash
# 3× ~30 s of NORMAL abuse: hard launches, brake-slams, tight turns, small bumps
python calibrate_thresholds.py --port COM5 --seconds 30 --label normal --out normal1.csv

# 3× crash runs into a cardboard box / wall
python calibrate_thresholds.py --port COM5 --seconds 15 --label crash --out crash1.csv
```

Each run prints mean / p99.9 / max G and a recommendation. Pick the threshold in the
gap:

```
CRASH_G_THRESHOLD  ≥  1.5 × (worst normal-driving max)
CRASH_G_THRESHOLD  ≤  0.7 × (typical crash peak)
```

**Read this before picking a number — there is a hard ceiling.**

The MPU6050 clips at **±16 g per axis**. Max possible magnitude is 16×√3 ≈ **27.7 g**.
A threshold at or above that can *never* fire, no matter how hard you hit it. (We
learned this the hard way with a threshold of 28.)

The firmware now reports **dynamic** acceleration — gravity is subtracted, so it reads
**0.00 g at rest** and the number is pure impact. Typical values on our rig:

| Condition | Dynamic G |
|---|---|
| Sitting still, **any orientation** | ~0.0 |
| Picked up / rotated by hand | under 2 (rotation can reach 2 g max) |
| Driving, vibration and bumps | ~1–3 |
| Hard hit into a box | ~4–15 (often rails the sensor) |

Default threshold is **5**. Keep it **above 2** so simply handling or rotating the board
can never fire it. Raise it if driving trips it; lower it if crashes are missed.

**If your readings look wrong** (non-zero at rest, or changing with orientation), the
boot line tells you immediately:

```
MSG,gravity=(0.012,-0.034,0.998) |g|=0.999  spread=0.004 g
```

`|g|` must be ~1.00. If it reads ~2.00 your `ACCEL_LSB_PER_G` doesn't match the
configured range; if `spread` is large the board moved during calibration.

**You don't need a high threshold to catch big hits.** Impacts hard enough to peg the
sensor are caught automatically by *saturation detection* — if an axis hits 95% of full
scale, that's an unambiguous crash and it fires regardless of the computed magnitude.
The alert is flagged so the AI tells the dispatcher that true severity exceeded what
the sensor could measure.
If crash spikes are too brief to catch, keep `IMPACT_MIN_SAMPLES` at 1 and lower the
threshold rather than raising sample count. Update `config.h`, reflash, verify:
5 normal runs with zero false alarms, 5 crashes with 5 detections. Log the numbers —
they are your evidence that the threshold is set correctly.

---

## 11. Conference demo runbook

**Night before:** phone hotspot tested with laptop; `.env` current; fresh ngrok URL;
`DRY_RUN=1` full rehearsal, then `DRY_RUN=0` live rehearsal; power bank + phones
charged; spare zip ties/tape; `victim_profile.json` filled with your chosen (fictional)
demo persona; phone that receives the call can play on speaker.

**Setup (10 min):** hotspot on → laptop on hotspot → `ngrok http 5000` → update
`PUBLIC_BASE_URL` → `python call_server.py` → `python bridge_ble.py` → **dashboard
open on the presenter's phone** (URL is printed by the server; mirror the phone to
the projector if possible) → car powered on the floor, still, until `armed`.

**Script (~4 min):**
1. Hold up the phone: live G dancing as the car drives — no false alarms
   (calibration story).
2. (Optional) drive at an obstacle — proximity warning blip from the buzzer.
3. Crash into the box → dashboard flashes the countdown, phone buzzes → **tap
   CANCEL**: "a conscious driver dismisses a false alarm."
4. Crash again → let the countdown run out on screen → dashboard flips to
   EMERGENCY — CALL IN PROGRESS → **the other phone rings on speaker** → briefing
   plays → presenter/audience ask dispatcher questions → "no further questions,
   goodbye." → tap RE-ARM.
5. Close on the architecture slide (use `crashguard-architecture.png`).

Two phones is the smoothest setup: one shows the dashboard, the other receives the
AI call. One phone also works — the dashboard keeps running behind the incoming call.

**Fallback ladder:** BLE won't link → Wi-Fi HTTP mode. Board acts up →
`python simulate_crash.py` (the call is the star anyway). No cell service →
`MOCK_AI=1 DRY_RUN=1` + console rehearsal on the projector.

---

## 11b. Map intelligence & incident documentation

**Map answers.** At the moment of the crash, before dialing, CrashGuard does one batch
of OpenStreetMap lookups from the live GPS: street address, city/county, nearest named
roads, and hospitals within 15 km (with distance + compass direction). That bundle goes
straight into the AI's context, so the dispatcher can ask things that were never in the
victim profile:

- *"What's the nearest cross street?"* → reads the nearest named roads
- *"What city is this?"* → city, county, state
- *"Where's the closest hospital?"* → name, distance, direction

One lookup per crash, not per question — so answers are instant, and it costs nothing
in AI credits. All free services, no API keys. Test it standalone:

```bash
python location_services.py 32.2809 -106.7469
```

**Documentation.** After a few exchanges the AI asks whether to document everything.
Say yes → it asks "text or email?" → say either (you can speak an address or number)
→ it sends the full incident table: timestamp, peak G, coordinates, address, cross
streets, nearest ER, occupant identity, blood type, conditions, allergies, meds,
contacts, vehicle.

**SMS works out of the box** on your Twilio number — nothing to configure.

**Email is opt-in.** If SMTP isn't set up, the AI won't offer email at all; it says
email isn't configured and offers to text instead. To enable it, add SMTP to `.env`
(Gmail: use an **App Password**, not your account password):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=your_app_password
REPORT_EMAIL=fallback@example.com
```

If the dispatcher picks email without giving an address, the AI **asks for it** and
understands spoken addresses — "chief at example dot gov" becomes `chief@example.gov`.
If they pick text without giving a number, it sends to the phone already on the call.

The yes/no, text/email, and address parsing are all handled locally, **not** by the AI
— instant, and zero credit cost.

**Gmail App Password setup:** Google Account → Security → 2-Step Verification (must be
on) → App passwords → generate one → paste the 16 characters into `SMTP_PASS` (spaces
are fine to remove). Your normal Gmail password will not work.

## 11c. Keeping AI credit usage low

Cost is dominated by the system prompt being re-billed every turn. Four things cut it:

| Measure | Effect |
|---|---|
| `claude-haiku-4-5` model | cheapest + fastest tier |
| Prompt caching on the system prompt | profile/scene billed at cached rate after turn 1 |
| `HISTORY_TURNS=6` | conversation context can't grow unbounded |
| Map data prefetched into context | AI never needs a second round trip to look something up |
| Documentation flow parsed locally | that whole feature costs zero credits |

A full demo call runs a fraction of a cent. Rehearse with `MOCK_AI=1` (free) and only
switch to live Claude for the real run.

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| `AccelAndGyro (0x69) not found` | Reseat JST; run the I2C scan; confirm 0x69 shows. |
| Dashboard says "board offline" | Bridge running and connected? Board powered? In Wi-Fi mode: board on the same hotspot, `SERVER_BASE_URL` = laptop's current IP? |
| Dashboard won't load on the phone | Phone on the same network as the laptop; use the LAN IP the server printed (not 127.0.0.1); laptop firewall may be blocking port 5000. |
| CANCEL tap does nothing | BLE mode: is the bridge running (it relays commands)? Watch for `[bridge] phone -> board: CANCEL`. Serial should show `EVT,DISARMED_BY_PHONE`. |
| Instant false triggers on power-up | Board moved during baseline; power-cycle and hold still; raise `ARM_DELAY_MS`. |
| False alarms while driving | Raise `CRASH_G_THRESHOLD` (we needed 80); add foam under the board; redo Stage 6. Driving vibration is the usual culprit — measure a normal-driving run and set the threshold well above its max. |
| AI can't answer cross street / hospital | Check the server log at trigger time for `cross streets:` / `nearest hospital:`. No internet at trigger = no map data. Test with `python location_services.py <lat> <lon>`. |
| Report never arrives | SMS: number must be verified on a Twilio trial. Email: SMTP_* set in `.env`, Gmail needs an App Password. Watch the server log for `documentation:`. |
| Crashes not detected | Lower threshold; confirm ±16 g line ran (it's in `setup()`); check `T,` lines actually spike. |
| Bridge can't find the board | Board powered? Laptop Bluetooth on? Another central already connected (only one at a time)? |
| Bridge won't reconnect after restart (used to need a board power-cycle) | Fixed in firmware: the board now re-advertises ~0.5 s after a disconnect, and drops a silent link after 12 s of no heartbeat. If you kill the bridge, just wait ~12 s before rerunning it — or exit with Ctrl+C, which disconnects cleanly and lets you rerun immediately. |
| Phone never rings | `python simulate_crash.py` — if that fails, read the server log: missing `.env` values are named explicitly. |
| Twilio error 11200 in console | `PUBLIC_BASE_URL` stale (ngrok restarted) or server not running. |
| AI answers feel off | Rehearse with `console_dispatcher_test.py`; refine `victim_profile.json`; the prompt lives in `dispatcher_agent.py`. |
| Speech misheard on the call | Quiet room, speak clearly; set `SPEECH_LANG`; keep questions short. |
| Serial port busy | Close Arduino Serial Monitor before running the calibration script. |
| Double phone calls | Cooldown already suppresses these (`TRIGGER_COOLDOWN_S`). |

---

## 13. Code walkthrough (where everything lives)

**`firmware/crashguard/crashguard.ino`** — one file, four layers:
- *Sampling:* `readAccelMag()` uses the official `AccelAndGyro` API (`getAccelX(false)`
  etc., cm/s²); `measureBaseline()` averages 100 still samples; the loop paces samples
  with `micros()` at `SAMPLE_HZ` and computes `liveG = |a| / baseline`.
- *State machine:* `ST_MONITORING → ST_COUNTDOWN → ST_ALERT_SENT / ST_DISARMED` in the
  `switch` in `loop()`. Countdown math, escalating beep cadence, debounced backup
  button, and `remoteCmd` — the one-shot slot the phone's CANCEL/REARM lands in
  (`doCancel()` / `doRearm()` are shared by phone and button paths).
- *Comms:* `sendCrashAlert()` builds `{"evt":"crash","peak_g":…}`; `buildStatusJson()`
  emits `{"g","pk","st","cd"}` at `STATUS_HZ`. BLE (`bleInit()`) exposes three
  characteristics — alerts `…0002`, status `…0003`, commands `…0004` (write) — with
  auto re-advertise; Wi-Fi mode POSTs status to `/telemetry` and reads any pending
  command out of the response.
- *Telemetry:* serial `T,…` lines at 20 Hz feed calibration; OLED draw functions
  survive behind `USE_OLED` for the bench.

**`host/dispatcher_agent.py`** — the AI's brain in one place: `build_intro()` writes
the opening briefing; `build_system_prompt()` embeds profile + crash JSON with hard
grounding rules (short spoken sentences, digits read out, "I don't have that
information" over guessing, demo-mode disclosure); `agent_reply()` calls the Claude
Messages API (`claude-sonnet-4-6`; see docs.claude.com for current models);
`_mock_reply()` is the offline keyword brain.

**`host/call_server.py`** — Flask + Twilio + the dashboard: `/trigger-call` and
`/crash-alert` (dedupe cooldown → outbound call), `/voice` (speak briefing, open
speech `<Gather>`), `/respond` (speech → agent → speak → listen again; closing
phrases → clean hangup), `/voice-reprompt` (silence handling), and the dashboard
family — `GET /` (the mobile page, inline HTML/CSS/JS polling at 4 Hz),
`POST /telemetry` (board status in, pending command out), `GET /status` (what the
phone polls, with a staleness flag), `POST /cancel` / `POST /rearm` (dashboard
buttons), `GET /pending-command` (what the BLE bridge polls), `/health`.

**`host/bridge_ble.py`** — bleak scanner keyed to the firmware UUIDs; subscribes to
*both* alert and status notifies (HTTP work runs on executor threads so a slow dial
never stalls BLE), dedupes repeated alert notifies, polls `/pending-command` every
250 ms and writes CANCEL/REARM to the board; auto-reconnect loop.

**Test/tuning trio** — `test_offline.py` (18-check webhook pipeline, zero accounts),
`console_dispatcher_test.py` (rehearse the call), `simulate_crash.py` (fake trigger),
`calibrate_thresholds.py` (serial capture + threshold recommendation).

---

## 14. Honest-engineering notes (be ready for judges' questions)

- **"Velocity"**: the proposal says the board tracks velocity. Integrating a consumer
  MEMS accelerometer drifts within seconds, so CrashGuard detects the *acceleration
  signature* of a collision instead — the same physical quantity (Δv at impact) that
  production automatic-crash-notification systems key on. The dashboard therefore
  shows impact G, not speed. Own this answer; it shows engineering judgment.
- **GPS**: the MYOSA Mini has no GNSS module. In deployment, location comes from the
  paired phone; in the demo it's preconfigured in `victim_profile.json`. The
  phone-app-bridge extension would make it live.
- **Why a web dashboard instead of a native BLE phone app**: works on every phone
  with zero installs, and iOS Safari has no Web Bluetooth anyway — routing phone ↔
  server ↔ (bridge) ↔ board over HTTP sidesteps that whole mess. The laptop bridge
  is standing exactly where a future companion app would.
- **"Calling emergency services"**: demo calls a team phone; `DEMO_MODE=1` makes the AI
  disclose the simulation if asked; Twilio is never to be pointed at real emergency
  numbers.
- **Grounding**: the agent can only see `victim_profile.json` + crash data, and is
  instructed to refuse to speculate — that's the difference between a useful emergency
  relay and a hallucination risk.
