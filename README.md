# MYOSA CrashGuard

Autonomous crash detection + AI emergency calling on the MYOSA Mini kit.
NMSU · MYOSA Event 6.0 (2026) · Aidan Garcia, Jacob Randall, Cabe Robertson, Vu Ha Nguyen · Mentor: Dr. Lavrova

**Crash detected → 10 s cancel countdown on a live PHONE DASHBOARD (big red CANCEL
button) → nobody cancels → AI calls the configured number, reports the occupant's
name / blood type / GPS / medical info, and answers the dispatcher's questions.**
(OnStar-style, demoed on an RC car — the car is moving, so all results show on the
phone, not on the board.)

## Start here
- **`TUTORIAL.md`** — full build/test/calibrate/demo walkthrough (stages 0–6 + runbook).
- Quick offline proof (no hardware, no accounts): `cd host && pip install -r requirements.txt && python test_offline.py`

## Layout
| Path | What it is |
|---|---|
| `firmware/crashguard/` | ESP32 sketch (`crashguard.ino`) + all tuning in `config.h` |
| `host/` | Flask call server + phone dashboard, Claude dispatcher agent, map lookups (cross streets/hospitals), incident report SMS/email, BLE bridge, test & calibration tools |
| `myosa-crashguard/` | **MYOSA submission** — mandatory Markdown format, the five images (cover, architecture, dashboard, board, team), and the demo video |

## Safety
The demo calls a **team member's phone**. Never configure `EMERGENCY_CONTACT_NUMBER`
to 911 or any real emergency service, and keep `DEMO_MODE=1` so the AI discloses the
simulation if asked.
