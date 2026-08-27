---
publishDate: 2026-08-26T00:00:00Z
title: Robust Vehicle Safety and Alert System Using AI - MYOSA CrashGuard
excerpt: An OnStar-style guardian built on the MYOSA Mini kit. It detects a collision on its own, gives the occupant ten seconds to cancel from a live phone dashboard, then places an AI phone call that reports who and where the victim is and answers the dispatcher's questions.
image: myosa-crashguard/crashguard-cover.jpg
tags:
  - iot
  - crash-detection
  - emergency-response
---

> When a crash happens and the driver cannot call for help, CrashGuard makes the call and answers the dispatcher's questions.

---

## Acknowledgements

We thank our faculty mentor **Dr. Lavrova** and the **Klipsch School of Electrical and Computer Engineering at New Mexico State University**, the **NMSU IEEE Student Branch**, and the **MYOSA initiative** (MakeSense EduTech, Pegasus Automation, and the IEEE Sensors Council) for the platform, the kit, and the opportunity to build this project for MYOSA Event 6.0.

**Team:** Aidan Garcia, Jacob Randall, Cabe Robertson, Vu Ha Nguyen - New Mexico State University.

---

## Overview

Every year, thousands of car crash victims suffer preventable deaths because emergency response is delayed, most critically when the driver is incapacitated and cannot call for help. CrashGuard closes that gap. It identifies a collision on its own from a sudden change in acceleration and contacts help without requiring any action from the occupant.

The MYOSA board becomes the brain of an in-vehicle guardian. It watches the accelerometer continuously. When it detects an impact, a live phone dashboard flashes a ten-second countdown while the buzzer on the car sounds. A conscious occupant taps one large red **CANCEL** button and the system re-arms, exactly how a driver waves off a pothole. If nobody cancels, CrashGuard assumes the occupant is incapacitated and escalates: it places a telephone call that speaks the victim's preloaded information and then answers the dispatcher's follow-up questions in natural conversation, grounded strictly in verified data.

**What problem it solves:** the minutes between a collision and the first call for help, when the person who would normally make that call cannot.

**Who it is for:** drivers who may be left unconscious or unable to reach a phone after a collision, and more broadly anyone exploring how an accessible IoT platform can automate a life-saving response.

**Key features:**

* Real-time impact detection at 100 Hz using gravity-compensated acceleration, so the reading is 0.00 g at rest in any orientation and the threshold is pure impact force
* Detection of impacts harder than the sensor can even measure, by treating sensor saturation as its own crash signature
* A live phone dashboard showing impact force, a ten-second cancel countdown, and a large CANCEL button, because the demo vehicle is moving and nobody can read a screen bolted to it
* Automatic, continuous GPS capture from the phone, with no button to press
* An AI dispatcher agent that answers questions using only the occupant profile, live crash data, and real map data
* Live map intelligence: street address, city and county, ZIP code, nearest cross streets, closest hospitals, and nearby landmarks with distance and compass bearing
* A complete written incident report, published to the dashboard and delivered by text message or email

---

## Demo / Examples

### Images

*Image paths follow the MYOSA blog asset convention (`/assets/images/myosa-crashguard/`). All image files are present in this folder alongside this Markdown file.*

<p align="center">
  <img src="crashguard-cover.jpg" width="800"><br/>
  <i>The MYOSA board and accelerometer module mounted on the RC demonstration vehicle</i>
</p>

<p align="center">
  <img src="crashguard-architecture.png" width="800"><br/>
  <i>End-to-end architecture: MYOSA board to BLE bridge to call server and Claude to Twilio to the dispatcher. The dashed path is the Wi-Fi fallback, which removes the bridge entirely.</i>
</p>

<p align="center">
  <img src="crashguard-dashboard.png" width="400"><br/>
  <i>The phone dashboard during the cancel countdown, showing live impact force and the CANCEL control</i>
</p>

<p align="center">
  <img src="crashguard-board.jpg" width="800"><br/>
  <i>The MYOSA motherboard, accelerometer module, and buzzer as mounted for the demonstration</i>
</p>

### Videos

<video controls width="100%">
  <source src="/crashguard-demo.mp4" type="video/mp4">
</video>

*The demonstration video is included in this folder as a local MP4 file:
[crashguard-demo.mp4](crashguard-demo.mp4). No external or YouTube links are used.*

---

## Features (Detailed)

### **1. Impact Detection in True Gravity-Compensated g**

The MYOSA Accelerometer and Gyroscope module (MPU6050, I2C address `0x69`) is sampled at 100 Hz with the full-scale range set to ±16 g.

At boot the firmware averages one hundred samples while the vehicle is still and records the **gravity vector**, not merely its magnitude. Every later sample has that vector subtracted, which yields the dynamic acceleration:

```plaintext
d(t) = ( a(t) - g0 ) / |g0|
```

This reads **0.00 g at rest in any orientation**. The threshold is therefore pure impact force with no gravity offset buried inside it, and because rotating the board by hand can produce at most 2 g of apparent dynamic acceleration, a threshold above 2 g cannot be triggered by ordinary handling. The system ships at **5.0 g**.

Readings pass through a median-of-three filter before comparison, which discards isolated outlier samples caused by bus glitches or momentary saturation, so both the displayed value and the trigger reflect sustained motion rather than electrical noise.

A collision is declared when the dynamic acceleration exceeds the threshold for a small number of consecutive samples.

### **2. Detecting Impacts Harder Than the Sensor Can Measure**

The MPU6050 saturates at ±16 g per axis, which bounds the largest reportable magnitude at 16 x sqrt(3), about 27.7 g. Impacts on a rigid demonstration platform readily exceed that, and a threshold set above the ceiling could never trigger at all.

CrashGuard therefore treats sustained per-axis railing as an **independent crash indicator**. When an axis reaches 95 percent of full scale for the confirmation window, an impact is declared regardless of the computed magnitude, and the alert is annotated to record that the true severity exceeded what the instrument could measure. Collisions too violent to measure are detected rather than missed.

The firmware also records which axis dominated and the sign of its component, distinguishing a deceleration (the vehicle striking something ahead) from an acceleration (being struck), and passes that classification to the AI agent.

### **3. The Sensor Acquisition Problem, and What It Taught Us**

This was the hardest bug in the project and the most useful lesson.

Our first version used the vendor library's per-axis accessors and produced nonsense: 14 to 17 g while the board sat perfectly still, changing with its orientation. Reading the library source explained why, and revealed three properties unsuitable for high-rate crash sensing:

1. Each per-axis accessor re-reads the range configuration register, tripling I2C traffic at a 100 Hz sample rate and returning intermittently corrupted samples.
2. The three axes are read in separate transactions and therefore correspond to different instants, so their vector magnitude is not physically meaningful during rapid motion.
3. The library's scale factor is twice the correct value for every full-scale range.

The firmware now performs a **single six-byte I2C burst read**, so all three axes are sampled at the same instant, and applies the MPU6050 datasheet sensitivity (2048 LSB/g at ±16 g) to the raw counts directly. **No gyroscope data is read at any point.** Angular rate contributes nothing to impact detection and would only add bus load.

The lesson generalises: a vendor library being convenient does not make it correct at the rate you need, and reading the source is what turned a broken sensor into a reliable one.

### **4. The Live Phone Dashboard**

The demonstration vehicle is in motion, so every result belongs on a phone rather than on a display bolted to the car.

The board streams its status five times per second, and the call server renders it as a mobile web page that any phone can open with no app installation. The dashboard shows the live impact force, the recent peak, the system state, and whether the board link is healthy.

The instant an impact is confirmed the dashboard flashes **!! CRASH DETECTED !!** with a full-screen countdown and a large red **CANCEL - I'M OK** button, and the phone vibrates. One tap cancels the alert and re-arms the system. The onboard pushbutton remains wired as a backup, and after an alert the dashboard offers a **RE-ARM** control.

Two distinct peaks are reported on purpose. The dashboard peak holds its highest recent value for three seconds and then follows the live reading again, so it shows current activity rather than a stuck maximum. The crash peak sent to the dispatcher is measured only across a 400 ms window around the impact, so the figure belongs to that collision rather than to the largest bump earlier in the session.

### **5. Automatic Location Capture**

A crash victim cannot press a button, so nothing about the location is manual. With the dashboard open, the phone streams its real GPS position continuously in the background using the browser geolocation API. The server always holds a current fix, ready the instant a crash fires.

The position is only re-sent when the phone has genuinely moved, which keeps the resolved address stable rather than flickering between neighbouring streets, and it is cached to disk so that restarting the server does not lose it. The dashboard displays whether the server currently holds a live fix, so the state is verifiable before a demonstration rather than discovered during one.

The MYOSA Mini has no GNSS module, and Bluetooth cannot provide geographic position. In a deployed system the location would come from the occupant's paired phone, which is exactly the role the dashboard plays here.

### **6. Live Map Intelligence**

A dispatcher asks things no stored profile can hold: what is the nearest cross street, what city is this, where is the closest emergency room.

At the moment of the crash CrashGuard issues a single batch of key-less OpenStreetMap queries against the live coordinates: Nominatim for the street address, city, county, state, and ZIP code, and one combined Overpass query returning the nearest named roads, hospitals within 15 km, and nearby landmarks such as schools, fuel stations, pharmacies, and police and fire stations. Distances and compass bearings are computed with the haversine formula. Several public Overpass mirrors are tried in turn, because a single endpoint frequently rate-limits.

Combining what were three separate queries into one cut the lookup time and the rate-limit exposure to roughly a third, which is what made cross streets reliable in practice.

The bundle is fetched once and injected into the agent's context. This bounds latency, because no network lookup happens during the conversation; it bounds cost, because the agent never needs an extra round trip to retrieve a fact; and it preserves correctness, because the agent reads authoritative map data instead of inferring street names. Fields that come back empty are marked explicitly as unavailable, so the agent states plainly that it does not have them rather than hedging.

### **7. The AI Emergency Caller**

The call server receives the crash event and places an outbound telephone call. The call opens with a spoken briefing assembled from the occupant profile and the live crash data: full legal name, GPS coordinates and street address, recorded peak impact, blood type, medical conditions, allergies, and the primary emergency contact.

The dispatcher's speech is then transcribed and passed to a Claude agent whose system prompt contains only the occupant profile, the crash data, and the map bundle. The agent is instructed to answer in one or two short spoken sentences, read numbers digit by digit, infer what the dispatcher actually needs, and say that it does not have a fact rather than ever guess. It has no camera or microphone in the cabin and is explicitly forbidden from inventing injuries.

Inference runs on a worker thread while the webhook returns immediately, so the telephony provider's webhook timeout cannot end a live call during a slow response.

### **8. Automated Incident Documentation**

After a few exchanges CrashGuard offers to document the incident, and it honours a request for the written record whenever it is spoken rather than only inside that scripted offer.

The report contains the detection timestamp, peak impact and impact direction, coordinates and street address, cross streets, nearest hospitals, occupant identity and date of birth, blood type, conditions, allergies, medications, emergency contacts, and vehicle description.

It is **always published to the phone dashboard first**, before any send is attempted, because that path cannot be blocked by a carrier and the report is therefore never lost. It is then delivered by text message or email to whatever destination the dispatcher gives. Spoken phone numbers and spoken email addresses are parsed locally, as are the yes/no and channel choices, so the entire feature adds no per-call inference cost and responds instantly.

Text delivery is verified rather than assumed: a queued message is not a delivered one, so the system polls until the message reaches a terminal status and reports the truth.

---

## Usage Instructions

Full setup, calibration, and demonstration instructions ship in `TUTORIAL.md` in the [project repository](https://github.com/caberobertson/CrashGuard). The short version follows.

**1. Flash the board.** Open the sketch in the Arduino IDE with the ESP32 core and the official MYOSA libraries installed, adjust `config.h` if your wiring differs, and upload. Hold the board still through the boot calibration, which learns the gravity vector.

```plaintext
firmware/crashguard/crashguard.ino
firmware/crashguard/config.h
```

**2. Configure and start the host.**

```bash
cd host
pip install -r requirements.txt
cp .env.example .env
ngrok http 5000
python call_server.py
python bridge_ble.py
```

Copy the HTTPS forwarding URL that ngrok prints into `PUBLIC_BASE_URL` in `.env`, then open that same HTTPS URL on a phone to reach the dashboard. The browser geolocation API requires HTTPS, so the plain local address will not capture position.

**3. Test with no hardware and no accounts.** Every check runs offline.

```plaintext
python test_offline.py
python console_dispatcher_test.py --mock
python simulate_crash.py --peak-g 6.0
```

**4. Calibrate the threshold on the vehicle.** Record labelled runs and set the threshold in the gap between them.

```plaintext
python calibrate_thresholds.py --port COM5 --seconds 30 --label normal
python calibrate_thresholds.py --port COM5 --seconds 15 --label crash
```

Typical values on our platform: at rest 0.0 g, handling and rotation under 2 g, driving 1 to 3 g, a deliberate crash into a box 4 to 15 g and frequently railing the sensor.

**5. Run the demonstration.** Drive, crash into a soft target, let the countdown expire, and answer the phone as the dispatcher.

---

## Tech Stack

* **MYOSA Mini Kit** - ESP32 motherboard and MPU6050 accelerometer module, with a passive buzzer and pushbutton added
* **Arduino C++** - firmware, official MYOSA sensor libraries, ESP32 BLE stack
* **Python 3, Flask** - call server, webhook handling, incident reporting
* **HTML, CSS, JavaScript** - the live mobile dashboard, served by the call server
* **bleak** - Bluetooth Low Energy bridge between the board and the host
* **pyserial** - serial telemetry capture for threshold calibration
* **OpenStreetMap Nominatim and Overpass** - reverse geocoding, cross streets, hospitals, and landmarks, with no API key required
* **Twilio Programmable Voice and Messaging** - outbound calling, speech recognition, neural text to speech, and SMS report delivery
* **Claude API** - the grounded conversational dispatcher agent

---

## Requirements / Installation

Install the ESP32 board package in the Arduino IDE, then install the official MYOSA libraries:

```bash
git clone https://github.com/myosa-sensors/arduino-libraries
```

Copy the `AccelAndGyro` folder from that repository into your `Arduino/libraries` directory and restart the IDE.

Install the host dependencies:

```bash
pip install flask twilio anthropic python-dotenv bleak pyserial requests
```

You will also need a Twilio account with a voice-capable number, an Anthropic API key, and ngrok to expose the call server to Twilio's webhooks. The offline test suite and the console rehearsal tool run without any account at all.

---

## File Structure

```
/myosa-crashguard
  ├─ firmware/
  │   └─ crashguard/
  │       ├─ crashguard.ino
  │       └─ config.h
  ├─ host/
  │   ├─ call_server.py
  │   ├─ dispatcher_agent.py
  │   ├─ location_services.py
  │   ├─ incident_report.py
  │   ├─ bridge_ble.py
  │   ├─ calibrate_thresholds.py
  │   ├─ simulate_crash.py
  │   ├─ console_dispatcher_test.py
  │   ├─ test_offline.py
  │   ├─ victim_profile.json
  │   └─ requirements.txt
  ├─ crashguard-cover.jpg
  ├─ crashguard-architecture.png
  ├─ crashguard-dashboard.png
  ├─ crashguard-demo.mp4
  ├─ TUTORIAL.md
  └─ myosa-crashguard.md
```

---

## License

MIT License. Free to use, modify, and learn from.

---

## Contribution Notes

Issues and pull requests are welcome. Good first contributions:

* A phone application to replace the laptop bridge, bringing the phone's own GPS directly into the alert path
* Rollover detection from the gravity vector already computed at calibration, by detecting when the measured gravity direction inverts
* Additional language support for the dispatcher agent
* An LTE alert path for genuinely standalone operation with no host nearby

---

## Safety Note

This is a demonstration system. The call is placed to a team member's phone that plays the dispatcher role, never to 911 or any real emergency service, and a demonstration flag makes the AI state clearly that no real emergency exists if it is asked.
