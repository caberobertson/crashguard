"""
call_server.py — CrashGuard's phone brain.

Flow:
  MYOSA board --(BLE via bridge_ble.py, or Wi-Fi HTTP)--> POST /trigger-call
  -> Twilio places an outbound call to EMERGENCY_CONTACT_NUMBER
  -> /voice speaks the emergency briefing (name, GPS, blood type, ...)
  -> /respond loops: dispatcher speech -> Claude -> spoken answer

SAFETY: point EMERGENCY_CONTACT_NUMBER at a team member's phone for the demo.
NEVER configure this to dial 911 or any real emergency service. Twilio numbers
must not be used to contact real emergency services for a demonstration.

Offline testing (no Twilio, no API key):
  DRY_RUN=1 MOCK_AI=1 python call_server.py     # then: python test_offline.py

Live testing:
  1. Fill in .env (copy .env.example)
  2. ngrok http 5000  -> put the https URL in PUBLIC_BASE_URL
  3. python call_server.py
  4. python simulate_crash.py --peak-g 5.2
"""

from __future__ import annotations

import os
import threading
import time

from dotenv import load_dotenv
from flask import Flask, Response, request

from dispatcher_agent import (
    agent_reply,
    build_intro,
    default_crash_context,
    load_profile,
)

load_dotenv()

app = Flask(__name__)

# ---- config from environment ------------------------------------------------
DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"          # 1 = never actually dial
MOCK_AI = os.getenv("MOCK_AI", "0") == "1"          # 1 = keyword brain, no API key
TTS_VOICE = os.getenv("TTS_VOICE", "Google.en-US-Neural2-F")
SPEECH_LANG = os.getenv("SPEECH_LANG", "en-US")
# How long Twilio waits after the speaker stops before sending their words.
# "auto" = Twilio decides (safest, waits for a natural pause). A number like
# "1" makes the AI jump in ~1 s after you stop — snappier, but can clip a
# slow talker. Tune during rehearsal: 1 or 2 feels responsive; auto is safe.
SPEECH_TIMEOUT = os.getenv("SPEECH_TIMEOUT", "1")
COOLDOWN_S = int(os.getenv("TRIGGER_COOLDOWN_S", "60"))
PORT = int(os.getenv("PORT", "5000"))

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM_NUMBER", "")
CALL_TO = os.getenv("EMERGENCY_CONTACT_NUMBER", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# ---- in-memory state --------------------------------------------------------
PROFILE = load_profile()
CRASH: dict = default_crash_context()               # replaced on each trigger
CONV: dict[str, list[dict]] = {}                    # CallSid -> message history
EMPTY_ROUNDS: dict[str, int] = {}                   # CallSid -> silent gathers
LAST_TRIGGER_TS = 0.0

# Live board status (fed by /telemetry, rendered by the phone dashboard).
# st: 0=MONITORING 1=COUNTDOWN 2=ALERT_SENT 3=DISARMED
BOARD = {"g": 1.0, "pk": 1.0, "st": 0, "cd": 0, "ts": 0.0}
PENDING_CMD: str | None = None                      # "CANCEL" | "REARM" -> board
CALL_PHASE = "idle"                                 # idle | dialing | dry-run
ANSWERS: dict[str, str | None] = {}                 # CallSid -> answer (None=computing)
WAIT_COUNT: dict[str, int] = {}                     # CallSid -> /reply poll count
# Live GPS pushed from the phone dashboard (overrides profile location per call)
LIVE_GPS: dict = {"lat": None, "lon": None, "nearest_address": None, "ts": 0.0}
_GPS_CACHE = ".last_location.json"      # survives a server restart


def _load_cached_gps() -> None:
    """Restore the last known phone position. The server is restarted often
    during setup, and without this it would sit with no location until the
    phone happened to move far enough to push a fresh fix."""
    try:
        import json as _j
        with open(_GPS_CACHE, "r", encoding="utf-8") as f:
            d = _j.load(f)
        if d.get("lat") is not None:
            LIVE_GPS.update(d)
            print(f"  restored last known location: {d.get('nearest_address') or d}")
    except Exception:
        pass


def _save_cached_gps() -> None:
    try:
        import json as _j
        with open(_GPS_CACHE, "w", encoding="utf-8") as f:
            _j.dump({k: LIVE_GPS[k] for k in ("lat", "lon", "nearest_address")}, f)
    except Exception:
        pass
SCENE: dict = {}                                    # map context for this crash
SCENE_READY = threading.Event()                     # set when map lookup finishes
SCENE_READY.set()
# documentation offer state per call: None | "offered" | "awaiting_delivery" | "done"
DOC_STATE: dict[str, str] = {}
TURN_COUNT: dict[str, int] = {}
LAST_REPORT: dict = {"text": None, "ts": 0.0, "delivery": None}

CLOSING_PHRASES = (
    "no further questions",
    "nothing else",
    "that's all",
    "that is all",
    "goodbye",
    "good bye",
    "hang up",
    "end the call",
    "we're done",
)


def twiml(vr) -> Response:
    return Response(str(vr), mimetype="text/xml")


def gather_block(vr, prompt: str | None = None):
    """Append a speech <Gather> that posts the dispatcher's words to /respond."""
    from twilio.twiml.voice_response import Gather

    g = Gather(
        input="speech",
        action="/respond",
        method="POST",
        speech_timeout=SPEECH_TIMEOUT,
        language=SPEECH_LANG,
    )
    if prompt:
        g.say(prompt, voice=TTS_VOICE)
    vr.append(g)
    vr.redirect("/voice-reprompt", method="POST")   # runs only if Gather hears nothing


# =============================================================================
# 1. Crash trigger — called by bridge_ble.py, the board (Wi-Fi mode), or
#    simulate_crash.py
# =============================================================================
def _enrich_scene() -> None:
    """Map lookups (address, cross streets, hospitals). Runs in the BACKGROUND
    while the phone rings: these can take many seconds, and holding the HTTP
    response open that long makes the BLE bridge time out and drop its link."""
    global SCENE
    try:
        from location_services import build_scene_context
        if LIVE_GPS.get("lat") is not None:
            lat, lon = LIVE_GPS["lat"], LIVE_GPS["lon"]
        else:
            g = PROFILE.get("gps", {})
            lat, lon = g.get("lat"), g.get("lon")
        if lat is None:
            return
        SCENE = build_scene_context(lat, lon)
        addr = ((SCENE.get("address") or {}).get("full")
                or LIVE_GPS.get("nearest_address"))
        if LIVE_GPS.get("lat") is not None:
            # The live coordinates always win, whether or not the address
            # resolved — a dispatcher can use raw coordinates, and falling back
            # to the preloaded location would be wrong.
            if addr:
                LIVE_GPS["nearest_address"] = addr
            PROFILE["gps"] = {"lat": lat, "lon": lon,
                              "nearest_address": addr or LIVE_GPS.get("nearest_address")}
        print(f"[scene] address: {addr}")
        if SCENE.get("cross_streets"):
            print(f"[scene] cross streets: {SCENE['cross_streets'][:3]}")
        if SCENE.get("nearby_hospitals"):
            print(f"[scene] nearest hospital: {SCENE['nearby_hospitals'][0]['name']}")
    except Exception as exc:  # noqa: BLE001
        print(f"[scene] lookup failed: {exc}")
    finally:
        SCENE_READY.set()


def _place_call() -> None:
    """Dial via Twilio, off the request path."""
    global CALL_PHASE
    try:
        from twilio.rest import Client
        call = Client(TWILIO_SID, TWILIO_TOKEN).calls.create(
            to=CALL_TO, from_=TWILIO_FROM,
            url=f"{PUBLIC_BASE_URL}/voice", method="POST")
        print(f"[trigger] dialing {CALL_TO} — call sid {call.sid}")
        CALL_PHASE = "dialing"
    except Exception as exc:  # noqa: BLE001
        msg, low, hint = str(exc), str(exc).lower(), ""
        if "authenticate" in low or "20003" in msg:
            hint = "check TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in .env"
        elif "21211" in msg or "21214" in msg:
            hint = "numbers must be E.164, e.g. +15755550142"
        elif "21608" in msg or "unverified" in low:
            hint = "Twilio trial: destination number must be verified"
        elif "21210" in msg:
            hint = "TWILIO_FROM_NUMBER must be a number you purchased"
        print(f"[trigger] TWILIO ERROR — {msg}" + (f"  -> {hint}" if hint else ""))
        CALL_PHASE = "idle"


def _handle_trigger(body: dict):
    global CRASH, LAST_TRIGGER_TS, CALL_PHASE, SCENE

    now = time.time()
    if now - LAST_TRIGGER_TS < COOLDOWN_S:
        return {"status": "ignored",
                "reason": f"cooldown ({COOLDOWN_S}s) — duplicate trigger suppressed"}, 409
    LAST_TRIGGER_TS = now

    peak_g = body.get("peak_g")
    decel = body.get("decel_g")
    CRASH = default_crash_context(
        peak_g=float(peak_g) if peak_g is not None else None,
        source=body.get("source", "unknown"),
        decel_g=float(decel) if decel is not None else None,
        axis=body.get("axis"),
        saturated=bool(body.get("saturated")),
    )
    print(f"[trigger] crash received: {CRASH}")

    # Apply the live position IMMEDIATELY — no network needed, we already hold
    # the coordinates, and /set-location has usually cached an address too.
    # Previously this only happened inside the slow background map lookup, so
    # the opening briefing could still be speaking the preloaded demo location.
    if LIVE_GPS.get("lat") is not None:
        PROFILE["gps"] = {
            "lat": LIVE_GPS["lat"],
            "lon": LIVE_GPS["lon"],
            "nearest_address": LIVE_GPS.get("nearest_address"),
        }
        print(f"[trigger] live position applied: {PROFILE['gps']}")
    else:
        print("[trigger] no live GPS yet — using preloaded profile location")

    SCENE = {}
    SCENE_READY.clear()

    if DRY_RUN:
        print("[trigger] DRY_RUN=1 — skipping the real phone call")
        CALL_PHASE = "dry-run"
        threading.Thread(target=_enrich_scene, daemon=True).start()
        return {"status": "dry-run", "crash": CRASH}, 200

    missing = [name for name, val in [
        ("TWILIO_ACCOUNT_SID", TWILIO_SID), ("TWILIO_AUTH_TOKEN", TWILIO_TOKEN),
        ("TWILIO_FROM_NUMBER", TWILIO_FROM), ("EMERGENCY_CONTACT_NUMBER", CALL_TO),
        ("PUBLIC_BASE_URL", PUBLIC_BASE_URL)] if not val]
    if missing:
        msg = f"missing env vars: {', '.join(missing)} (set them in .env, or use DRY_RUN=1)"
        print(f"[trigger] ERROR — {msg}")
        SCENE_READY.set()
        return {"status": "error", "reason": msg}, 500

    # Dial and enrich in PARALLEL, both off this request, so the bridge gets an
    # instant answer and its BLE link is never disturbed.
    threading.Thread(target=_place_call, daemon=True).start()
    threading.Thread(target=_enrich_scene, daemon=True).start()
    return {"status": "accepted", "crash": CRASH}, 202


@app.post("/trigger-call")
def trigger_call():
    return _handle_trigger(request.get_json(silent=True) or {})


@app.post("/crash-alert")          # Wi-Fi-mode boards POST the alert here
def crash_alert():
    body = request.get_json(silent=True) or {}
    body.setdefault("source", "wifi")
    return _handle_trigger(body)


# =============================================================================
# 2. Call connects — speak the emergency briefing, then open the floor
# =============================================================================
@app.post("/voice")
def voice():
    from twilio.twiml.voice_response import VoiceResponse

    sid = request.form.get("CallSid", "test")
    CONV[sid] = []
    EMPTY_ROUNDS[sid] = 0

    # The map lookup started when the crash fired and has been running while
    # the phone rang. Give it a short grace period so the opening briefing can
    # name a street rather than only reading coordinates.
    SCENE_READY.wait(timeout=float(os.getenv("SCENE_WAIT_S", "3")))

    intro = build_intro(PROFILE, CRASH, DEMO_MODE)
    # Seed history so the model knows what has already been said on the call.
    CONV[sid].append({"role": "user", "content": "[call connected]"})
    CONV[sid].append({"role": "assistant", "content": intro})

    vr = VoiceResponse()
    vr.say(intro, voice=TTS_VOICE)
    gather_block(vr)
    return twiml(vr)


# =============================================================================
# 3. Dispatcher spoke — answer with the AI agent, then listen again
# =============================================================================
@app.errorhandler(500)
def _keep_call_alive(err):
    """Any unhandled error during a call would otherwise make Twilio play
    'an error has occurred, goodbye' and hang up. Instead, apologise briefly
    and keep listening so the demo survives."""
    from twilio.twiml.voice_response import VoiceResponse
    print(f"[server] unhandled error, keeping call alive: {err}")
    vr = VoiceResponse()
    vr.say("One moment. Please repeat your question.", voice=TTS_VOICE)
    gather_block(vr)
    return twiml(vr)


@app.post("/respond")
def respond():
    from twilio.twiml.voice_response import VoiceResponse

    sid = request.form.get("CallSid", "test")
    speech = (request.form.get("SpeechResult") or "").strip()
    vr = VoiceResponse()

    if sid not in CONV:               # server restarted mid-call
        CONV[sid] = []
        EMPTY_ROUNDS[sid] = 0

    if not speech:
        gather_block(vr, "I did not catch that. Please repeat your question.")
        return twiml(vr)

    print(f"[{sid}] dispatcher: {speech}")

    # ---- Documentation flow. Handled with plain logic (NO AI call) so it
    #      costs zero credits and answers instantly. ----
    # Imported defensively: if incident_report.py is an older version, the call
    # must NOT die with "an error has occurred" — documentation simply becomes
    # unavailable and the conversation continues.
    try:
        from incident_report import (build_report, extract_phone,
                                     normalize_spoken_email, parse_delivery,
                                     send_email, send_sms, smtp_configured,
                                     wants_documentation, wants_report)
        _DOC_OK = True
    except ImportError as exc:
        print(f"[{sid}] documentation features unavailable — {exc}")
        print("      -> incident_report.py is out of date; replace the whole "
              "host/ folder from the latest zip.")
        _DOC_OK = False

    stage = DOC_STATE.get(sid) if _DOC_OK else None

    def _deliver(channel: str, dest: str) -> None:
        """Send the report and speak the outcome honestly."""
        report = build_report(PROFILE, CRASH, SCENE)

        # ALWAYS publish to the dashboard first. Carriers can block SMS and
        # SMTP can be unconfigured, but the dashboard always works — so the
        # report is never lost regardless of what happens next.
        LAST_REPORT["text"] = report
        LAST_REPORT["ts"] = time.time()

        if channel == "sms":
            ok, detail = send_sms(dest, report)
            where = f"by text to {dest}"
        else:
            ok, detail = send_email(dest, report)
            where = f"to {dest}"
        LAST_REPORT["delivery"] = detail
        print(f"[{sid}] documentation: {detail}")
        DOC_STATE[sid] = "done"

        if ok:
            vr.say(f"Sent {where}. It includes the location, cross streets, "
                   "occupant identity, medical information, and emergency "
                   "contacts.", voice=TTS_VOICE)
        else:
            # Do not claim success. The report IS available on the dashboard.
            vr.say("I could not get that through to your device, but the full "
                   "written report is now published on the CrashGuard incident "
                   "page, and I can read any part of it to you now.",
                   voice=TTS_VOICE)

    # A request for the written record is honoured WHENEVER it is spoken, not
    # only inside the scripted offer. If a destination is included, act on it
    # immediately; otherwise ask for the missing piece.
    if _DOC_OK and stage not in ("awaiting_delivery", "awaiting_email",
                                 "awaiting_number") and wants_report(speech):
        channel, dest = parse_delivery(speech)
        if channel == "sms":
            if dest:
                _deliver("sms", dest)
            else:
                DOC_STATE[sid] = "awaiting_number"
                vr.say("What number should I text it to?", voice=TTS_VOICE)
            gather_block(vr)
            return twiml(vr)
        if channel == "email" and smtp_configured():
            if dest:
                _deliver("email", dest)
            else:
                DOC_STATE[sid] = "awaiting_email"
                vr.say("What email address should I send it to?", voice=TTS_VOICE)
            gather_block(vr)
            return twiml(vr)
        DOC_STATE[sid] = "awaiting_delivery"
        vr.say("I can send the full report. Text message"
               + (", or email?" if smtp_configured() else "?"), voice=TTS_VOICE)
        gather_block(vr)
        return twiml(vr)

    if stage == "awaiting_number":
        num = extract_phone(speech)
        if num:
            _deliver("sms", num)
        else:
            vr.say("I did not catch that number. Please say the ten digits.",
                   voice=TTS_VOICE)
        gather_block(vr)
        return twiml(vr)

    if stage == "offered":
        ans = wants_documentation(speech)
        if ans is True:
            DOC_STATE[sid] = "awaiting_delivery"
            vr.say("Understood. Should I send it by text message"
                   + (", or by email?" if smtp_configured() else "?"),
                   voice=TTS_VOICE)
            gather_block(vr)
            return twiml(vr)
        if ans is False:
            DOC_STATE[sid] = "done"
            vr.say("Understood, no report will be sent.", voice=TTS_VOICE)
            gather_block(vr)
            return twiml(vr)
        # unclear — fall through to the AI

    if stage == "awaiting_email":
        addr = normalize_spoken_email(speech)
        if addr:
            _deliver("email", addr)
            gather_block(vr)
            return twiml(vr)
        vr.say("I did not catch that address. You can spell it out, "
               "or say text message instead.", voice=TTS_VOICE)
        gather_block(vr)
        return twiml(vr)

    if stage == "awaiting_delivery":
        channel, dest = parse_delivery(speech)

        if channel == "email":
            if not smtp_configured():
                vr.say("Email is not configured on this unit. I can send it by "
                       "text message instead. What number should I use?",
                       voice=TTS_VOICE)
                DOC_STATE[sid] = "awaiting_number"
                gather_block(vr)
                return twiml(vr)
            if dest:
                _deliver("email", dest)
            else:
                DOC_STATE[sid] = "awaiting_email"
                vr.say("What email address should I send it to?", voice=TTS_VOICE)
            gather_block(vr)
            return twiml(vr)

        if channel == "sms":
            if dest:
                _deliver("sms", dest)
            else:
                DOC_STATE[sid] = "awaiting_number"
                vr.say("What number should I text it to?", voice=TTS_VOICE)
            gather_block(vr)
            return twiml(vr)

        vr.say("Text message, or email?", voice=TTS_VOICE)
        gather_block(vr)
        return twiml(vr)

    CONV[sid].append({"role": "user", "content": speech})

    if any(p in speech.lower() for p in CLOSING_PHRASES):
        vr.say("Understood. CrashGuard signing off. Emergency data remains "
               "available if you call back. Goodbye.", voice=TTS_VOICE)
        vr.hangup()
        return twiml(vr)

    # Compute the answer in a background thread so this webhook returns to
    # Twilio immediately (Twilio times out a held call at ~15 s; a slow model
    # round trip would otherwise play "an application error has occurred").
    ANSWERS[sid] = None
    threading.Thread(target=_compute_answer, args=(sid,), daemon=True).start()

    # Brief head start: most Haiku replies land fast, so a short wait here
    # often lets /reply speak on the very first poll — minimal dead air, while
    # still returning to Twilio fast enough to never time out.
    time.sleep(0.25)
    vr.redirect("/reply", method="POST")
    return twiml(vr)


def _compute_answer(sid: str) -> None:
    """Runs off the webhook path; result is picked up by /reply."""
    try:
        answer = agent_reply(CONV[sid], PROFILE, CRASH,
                             demo_mode=DEMO_MODE, mock=MOCK_AI, scene=SCENE)
    except Exception as exc:  # keep the call alive even if the API hiccups
        print(f"[{sid}] agent error: {exc}")
        answer = (
            "I am having trouble processing that. The occupant is "
            f"{PROFILE.get('full_name')}, located near "
            f"{PROFILE.get('gps', {}).get('nearest_address', 'an unknown address')}."
        )
    CONV[sid].append({"role": "assistant", "content": answer})

    # Safety net: the system CAN deliver the written report, so if the model
    # ever claims otherwise, replace that answer and start the delivery flow.
    try:
        from incident_report import is_send_refusal, smtp_configured
        if is_send_refusal(answer):
            print(f"[{sid}] overrode a false 'cannot send' reply")
            answer = ("I can send the full written report. Text message"
                      + (", or email?" if smtp_configured() else "?"))
            CONV[sid][-1]["content"] = answer
            DOC_STATE[sid] = "awaiting_delivery"
    except Exception:
        pass

    # After a few exchanges, proactively offer to document everything. The
    # offer text is fixed (no extra AI tokens) and the dispatcher's reply is
    # parsed locally, so this whole feature costs nothing in credits.
    TURN_COUNT[sid] = TURN_COUNT.get(sid, 0) + 1
    if TURN_COUNT[sid] >= int(os.getenv("DOC_OFFER_AFTER_TURNS", "3")) \
            and DOC_STATE.get(sid) is None:
        DOC_STATE[sid] = "offered"
        answer += (" Would you like me to document all of this information "
                   "and send it to you?")

    print(f"[{sid}] crashguard: {answer}")
    ANSWERS[sid] = answer


@app.post("/reply")
def reply():
    """Speaks the answer once the worker has it; short self-redirects while
    it's still thinking. Each hop returns instantly, so Twilio never times
    out even if the model takes several seconds."""
    from twilio.twiml.voice_response import VoiceResponse

    sid = request.form.get("CallSid", "test")
    vr = VoiceResponse()
    answer = ANSWERS.get(sid, "__missing__")

    if answer is None:                 # still computing
        waits = WAIT_COUNT.get(sid, 0) + 1
        WAIT_COUNT[sid] = waits
        if waits > 40:                 # ~10 s safety net — don't loop forever
            ANSWERS[sid] = ("I am still gathering that. The occupant is "
                            f"{PROFILE.get('full_name')}.")
        # short server-side wait instead of a 1 s <Pause>: tight cadence so the
        # answer is spoken within a fraction of a second of being ready
        time.sleep(0.25)
        vr.redirect("/reply", method="POST")
        return twiml(vr)

    WAIT_COUNT[sid] = 0
    if answer == "__missing__":        # server restarted mid-call
        gather_block(vr, "Please repeat your question.")
        return twiml(vr)

    ANSWERS.pop(sid, None)
    vr.say(answer, voice=TTS_VOICE)
    gather_block(vr)
    return twiml(vr)


# =============================================================================
# 4. Silence handling — nudge, then repeat the critical facts once
# =============================================================================
@app.post("/voice-reprompt")
def voice_reprompt():
    from twilio.twiml.voice_response import VoiceResponse

    sid = request.form.get("CallSid", "test")
    EMPTY_ROUNDS[sid] = EMPTY_ROUNDS.get(sid, 0) + 1
    vr = VoiceResponse()

    if EMPTY_ROUNDS[sid] >= 3:
        vr.say("No response detected. CrashGuard signing off. Goodbye.", voice=TTS_VOICE)
        vr.hangup()
    elif EMPTY_ROUNDS[sid] == 2:
        vr.say("Repeating critical information.", voice=TTS_VOICE)
        vr.say(build_intro(PROFILE, CRASH, DEMO_MODE), voice=TTS_VOICE)
        gather_block(vr)
    else:
        gather_block(vr, "Are you still there? I can answer questions about the occupant.")
    return twiml(vr)


# =============================================================================
# 5. Phone dashboard — the car is moving, so ALL results render on the phone.
#    Board streams status here (via the BLE bridge, or directly in Wi-Fi mode);
#    the phone polls /status and can send CANCEL / REARM back to the board.
# =============================================================================
@app.post("/telemetry")
def telemetry():
    """Board status in; any pending phone command out (Wi-Fi mode uses the
    response to learn about CANCEL/REARM — bounded by the board's STATUS_HZ)."""
    global PENDING_CMD, CALL_PHASE
    body = request.get_json(silent=True) or {}
    for k in ("g", "pk", "st", "cd"):
        if k in body:
            BOARD[k] = body[k]
    BOARD["ts"] = time.time()
    if BOARD.get("st") == 0 and CALL_PHASE in ("dry-run",):
        CALL_PHASE = "idle"                    # board re-armed after a dry run
    cmd, PENDING_CMD = PENDING_CMD, None
    return {"command": cmd or "none"}


@app.get("/pending-command")
def pending_command():
    """BLE bridge polls this and writes the command to the board's CMD char."""
    global PENDING_CMD
    cmd, PENDING_CMD = PENDING_CMD, None
    return {"command": cmd or "none"}


@app.post("/cancel")
def cancel():
    global PENDING_CMD
    PENDING_CMD = "CANCEL"
    print("[dashboard] CANCEL requested from phone")
    return {"status": "ok", "command": "CANCEL"}


@app.post("/rearm")
def rearm():
    global PENDING_CMD, CALL_PHASE
    PENDING_CMD = "REARM"
    CALL_PHASE = "idle"
    print("[dashboard] REARM requested from phone")
    return {"status": "ok", "command": "REARM"}


@app.get("/report")
def get_report():
    """The written incident report, for the phone dashboard. This path cannot
    be blocked by a carrier, so it always works even when SMS is filtered."""
    return {"text": LAST_REPORT["text"], "delivery": LAST_REPORT["delivery"],
            "ts": LAST_REPORT["ts"]}


@app.get("/status")
def status():
    stale = (time.time() - BOARD["ts"]) > 2.5 if BOARD["ts"] else True
    return {**BOARD, "stale": stale, "call": CALL_PHASE,
            "occupant": PROFILE.get("full_name"),
            "gps_live": LIVE_GPS.get("lat") is not None,
            "gps_addr": (PROFILE.get("gps") or {}).get("nearest_address")}


DASHBOARD_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>CrashGuard</title>
<style>
  :root { --bg:#0b0f14; --card:#141b23; --line:#243040; --txt:#e8eef5;
          --dim:#8a99ab; --ok:#2ecc71; --warn:#f1c40f; --bad:#ff3b30; }
  * { box-sizing:border-box; margin:0; -webkit-tap-highlight-color:transparent; }
  body { background:var(--bg); color:var(--txt); min-height:100vh;
         font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         display:flex; flex-direction:column; padding:16px; gap:14px; }
  header { display:flex; align-items:center; justify-content:space-between; }
  header h1 { font-size:19px; letter-spacing:.4px; }
  #link { display:flex; align-items:center; gap:7px; color:var(--dim); font-size:13px; }
  #dot { width:10px; height:10px; border-radius:50%; background:var(--bad); }
  #banner { border-radius:14px; padding:13px; text-align:center; font-size:17px;
            font-weight:700; letter-spacing:.6px; background:var(--card);
            border:1px solid var(--line); }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:16px; padding:18px; text-align:center; }
  .label { color:var(--dim); font-size:12px; text-transform:uppercase;
           letter-spacing:1.5px; margin-bottom:4px; }
  #g { font-size:88px; font-weight:800; line-height:1;
       font-variant-numeric:tabular-nums; }
  #g small { font-size:26px; color:var(--dim); font-weight:600; }
  #peakrow { display:flex; justify-content:space-around; }
  #peakrow div { flex:1; }
  #peakrow .v { font-size:30px; font-weight:700; font-variant-numeric:tabular-nums; }
  #cdwrap { display:none; }
  #cd { font-size:120px; font-weight:900; line-height:1; color:var(--bad);
        font-variant-numeric:tabular-nums; animation:pulse .5s infinite alternate; }
  @keyframes pulse { from{transform:scale(1)} to{transform:scale(1.06)} }
  button { width:100%; border:0; border-radius:16px; padding:22px;
           font-size:24px; font-weight:800; letter-spacing:1px; color:#fff; }
  #btnCancel { background:var(--bad); display:none; box-shadow:0 0 24px #ff3b3055; }
  #btnRearm  { background:#2f6fed; display:none; }
  button:active { filter:brightness(.85); }
  footer { color:var(--dim); font-size:12px; text-align:center; margin-top:auto; }
</style></head>
<body>
  <header>
    <h1>CrashGuard</h1>
    <div id="link"><div id="dot"></div><span id="linktxt">board offline</span></div>
  </header>

  <div id="banner">CONNECTING…</div>

  <div class="card" id="cdwrap">
    <div class="label">Impact detected — cancel window</div>
    <div id="cd">10</div>
  </div>
  <button id="btnCancel" onclick="post('/cancel')">CANCEL — I'M OK</button>
  <button id="btnRearm"  onclick="post('/rearm')">RE-ARM SYSTEM</button>

  <div class="card">
    <div class="label">Impact force</div>
    <div id="g">1.00<small> G</small></div>
  </div>
  <div class="card" id="peakrow">
    <div><div class="label">Peak</div><div class="v" id="pk">1.00 G</div></div>
    <div><div class="label">Occupant</div><div class="v" id="occ" style="font-size:17px">—</div></div>
  </div>

  <div class="card" id="loccard" style="padding:12px">
    <div class="label">Location (auto — sent to dispatcher)</div>
    <div class="v" id="loc" style="font-size:15px">acquiring GPS…</div>
    <div id="locsrv" style="font-size:12px;margin-top:6px;color:var(--bad)">
      server has NOT received your location yet</div>
  </div>

  <div class="card" id="repcard" style="display:none;text-align:left">
    <div class="label" style="text-align:center">Incident report</div>
    <pre id="rep" style="white-space:pre-wrap;font-size:11px;line-height:1.35;
         color:var(--txt);margin:8px 0 0;font-family:ui-monospace,Menlo,monospace"></pre>
    <div id="repdel" style="color:var(--dim);font-size:11px;margin-top:8px"></div>
  </div>

  <footer>MYOSA CrashGuard · NMSU · demo system — never dials real emergency services</footer>

<script>
  const $ = id => document.getElementById(id);
  const STATES = ["MONITORING — ARMED","!! CRASH DETECTED !!",
                  "EMERGENCY — AI CALLING","CANCELED — RE-ARMING"];
  const COLORS  = [null,"var(--bad)",null,null];
  let lastSt = -1;

  function post(p){ fetch(p,{method:"POST"}); if(navigator.vibrate)navigator.vibrate(30); }

  // Automatic, continuous location — no button. A crash victim can't tap
  // anything, so the phone streams its GPS in the background and the server
  // always has the latest fix ready the instant a crash fires.
  //
  // Stability: consumer GPS jitters several metres even when still, which
  // makes the geocoder flip between neighbouring addresses. So we only push a
  // new fix when the phone has actually MOVED a meaningful distance from the
  // last one we sent (or on the very first good fix). A parked demo car then
  // locks onto ONE address instead of flickering.
  let sent = null;                    // {lat, lon} last accepted by the server
  let lastFix = null;                 // most recent fix from the GPS, always kept
  let lastPush = 0;                   // when we last posted to the server

  function metresBetween(a, b){
    const R = 6371000, toRad = d => d*Math.PI/180;
    const dLat = toRad(b.lat-a.lat), dLon = toRad(b.lon-a.lon);
    const s = Math.sin(dLat/2)**2 +
              Math.cos(toRad(a.lat))*Math.cos(toRad(b.lat))*Math.sin(dLon/2)**2;
    return 2*R*Math.asin(Math.sqrt(s));
  }

  function startLocation(){
    const el = $("loc");
    if(!navigator.geolocation){ el.textContent = "GPS not supported — using preloaded"; return; }
    navigator.geolocation.watchPosition(async pos => {
      const {latitude:lat, longitude:lon, accuracy} = pos.coords;
      const here = {lat, lon};

      lastFix = here;               // remember it even if we do not send now

      // Ignore very poor fixes (>50 m) unless we have nothing at all yet.
      if(accuracy > 50 && sent){ return; }

      // Send the first fix, or once we have genuinely moved >25 m, or every
      // 30 s as a refresh. The refresh matters because the server keeps the
      // position in memory only: restarting it would otherwise leave it with
      // no location until the phone happened to move.
      const stale = (Date.now() - lastPush) > 30000;
      if(sent && !stale && metresBetween(sent, here) < 25){ return; }

      try{
        const r = await fetch("/set-location", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({lat, lon})
        });
        const d = await r.json();
        sent = here; lastPush = Date.now();
        el.textContent = d.nearest_address || `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
      }catch(e){ el.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)} (server unreachable)`; }
    }, err => {
      $("loc").textContent = (err.code===1)
        ? "location permission denied — using preloaded"
        : "GPS unavailable — using preloaded";
    }, {enableHighAccuracy:true, timeout:10000, maximumAge:2000});
  }

  async function tick(){
    try{
      const s = await (await fetch("/status")).json();
      $("dot").style.background = s.stale ? "var(--bad)" : "var(--ok)";
      $("linktxt").textContent  = s.stale ? "board offline" : "board live";
      $("g").innerHTML  = (+s.g).toFixed(2) + "<small> G</small>";
      $("pk").textContent = (+s.pk).toFixed(2) + " G";
      $("occ").textContent = s.occupant || "—";
      const ls = $("locsrv");
      if(!s.gps_live && lastFix){
        // Server lost the position (most often: it was restarted). Re-send
        // immediately instead of waiting for the phone to move.
        fetch("/set-location", {method:"POST",
          headers:{"Content-Type":"application/json"},
          body: JSON.stringify(lastFix)}).then(()=>{ lastPush = Date.now(); })
          .catch(()=>{});
      }
      if(s.gps_live){
        ls.style.color = "var(--ok)";
        ls.textContent = "server has your live location ✓";
      }else{
        ls.style.color = "var(--bad)";
        ls.textContent = "server has NOT received your location — "
          + (location.protocol === "https:"
             ? "allow location permission"
             : "open this page over the HTTPS ngrok URL");
      }

      const st = s.stale ? -1 : s.st;
      $("banner").textContent = s.stale ? "WAITING FOR BOARD…" : STATES[st] || "…";
      $("banner").style.borderColor = COLORS[st] || "var(--line)";
      $("banner").style.color = (st===1) ? "var(--bad)"
                              : (st===2) ? "var(--warn)" : "var(--txt)";

      const countdown = (st===1);
      $("cdwrap").style.display    = countdown ? "block" : "none";
      $("btnCancel").style.display = countdown ? "block" : "none";
      $("cd").textContent = s.cd;
      $("btnRearm").style.display  = (st===2) ? "block" : "none";

      if(st===1 && lastSt!==1 && navigator.vibrate) navigator.vibrate([200,80,200]);
      if(st===2 && s.call==="dialing")
        $("banner").textContent = "EMERGENCY — CALL IN PROGRESS";
      lastSt = st;
    }catch(e){
      $("dot").style.background="var(--bad)";
      $("linktxt").textContent="server unreachable";
    }
  }
  async function pollReport(){
    try{
      const r = await (await fetch("/report")).json();
      if(r && r.text){
        $("repcard").style.display = "block";
        $("rep").textContent = r.text;
        $("repdel").textContent = r.delivery ? ("delivery: " + r.delivery) : "";
      }
    }catch(e){}
  }
  setInterval(pollReport, 2000); pollReport();

  setInterval(tick, 250); tick();
  startLocation();   // begin streaming GPS automatically — no user action
</script>
</body></html>"""


@app.get("/")
@app.get("/dashboard")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


def _shorten_address(data: dict) -> str | None:
    """Turn Nominatim's long display_name into a short spoken address, e.g.
    '1780 East University Avenue, Las Cruces'. Falls back to display_name."""
    a = data.get("address", {}) if isinstance(data, dict) else {}
    house = a.get("house_number", "")
    road = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
    city = (a.get("city") or a.get("town") or a.get("village")
            or a.get("hamlet") or a.get("suburb") or "")
    street = f"{house} {road}".strip()
    parts = [p for p in (street, city) if p]
    if parts:
        return ", ".join(parts)
    return data.get("display_name") if isinstance(data, dict) else None


def _reverse_geocode(lat: float, lon: float) -> str | None:
    """Coordinates -> short spoken address. Tries two free services so one
    being slow or blocking us doesn't cost the demo its address. Returns None
    if both fail (caller then speaks the raw coordinates)."""
    import urllib.request
    import urllib.parse
    import json as _json

    # Provider 1: Nominatim (OpenStreetMap)
    try:
        q = urllib.parse.urlencode({
            "format": "jsonv2", "lat": lat, "lon": lon, "zoom": "18",
            "addressdetails": "1",
        })
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{q}",
            headers={"User-Agent": "MYOSA-CrashGuard/1.0 (student demo project)",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            addr = _shorten_address(_json.load(resp))
            if addr:
                return addr
    except Exception as exc:  # noqa: BLE001
        print(f"[location] nominatim failed: {exc}")

    # Provider 2: BigDataCloud (no key, permissive, reliable)
    try:
        q = urllib.parse.urlencode({"latitude": lat, "longitude": lon,
                                    "localityLanguage": "en"})
        req = urllib.request.Request(
            "https://api.bigdatacloud.net/data/reverse-geocode-client?" + q,
            headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            d = _json.load(resp)
        street = (d.get("localityInfo", {}) or {})
        # build "<locality>, <city>" from whatever fields are present
        city = d.get("city") or d.get("locality") or ""
        principal = d.get("principalSubdivision") or ""
        parts = [p for p in (d.get("locality"), city or principal) if p]
        # de-dup if locality == city
        seen = []
        for p in parts:
            if p not in seen:
                seen.append(p)
        if seen:
            return ", ".join(seen)
    except Exception as exc:  # noqa: BLE001
        print(f"[location] bigdatacloud failed: {exc}")

    return None


@app.post("/set-location")
def set_location():
    """The phone dashboard posts its real GPS here (from the browser's
    Geolocation API). Stored as an override for the next crash call."""
    body = request.get_json(silent=True) or {}
    try:
        lat = float(body["lat"])
        lon = float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return {"status": "error", "reason": "need numeric lat and lon"}, 400

    LIVE_GPS["lat"] = round(lat, 6)
    LIVE_GPS["lon"] = round(lon, 6)
    LIVE_GPS["ts"] = time.time()

    # Best-effort reverse geocode (two providers). Never blocks the demo:
    # if both fail, addr stays None and the briefing speaks the coordinates.
    addr = _reverse_geocode(lat, lon)
    LIVE_GPS["nearest_address"] = addr

    _save_cached_gps()
    print(f"[location] live phone GPS set: {LIVE_GPS['lat']}, {LIVE_GPS['lon']}"
          + (f" ({addr})" if addr else ""))
    return {"status": "ok", "lat": LIVE_GPS["lat"], "lon": LIVE_GPS["lon"],
            "nearest_address": addr}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "demo_mode": DEMO_MODE,
        "dry_run": DRY_RUN,
        "mock_ai": MOCK_AI,
        "occupant": PROFILE.get("full_name"),
    }


def _startup_selfcheck() -> None:
    """Catch mismatched file versions BEFORE a demo instead of mid-call.
    Updating one module but not another used to raise ImportError inside a
    live call, which Twilio turns into 'an error has occurred, goodbye'."""
    import sys
    print(f"  python  : {sys.executable}")
    print(f"  workdir : {os.getcwd()}")
    required = {
        "incident_report": ["build_report", "extract_phone", "parse_delivery",
                            "send_email", "send_sms", "smtp_configured",
                            "wants_documentation", "wants_report",
                            "normalize_spoken_email", "is_send_refusal"],
        "location_services": ["build_scene_context", "context_summary"],
        "dispatcher_agent": ["agent_reply", "build_intro", "load_profile",
                             "default_crash_context"],
    }
    problems = []
    for pkg, why in (("flask", "web server"), ("twilio", "phone calls and SMS"),
                     ("dotenv", "reading .env"), ("anthropic", "live AI replies")):
        try:
            __import__(pkg)
        except ImportError:
            optional = pkg == "anthropic" and MOCK_AI
            if not optional:
                problems.append(
                    f"  package '{pkg}' is NOT installed ({why}) — "
                    f"run: pip install -r requirements.txt")
    for mod_name, names in required.items():
        try:
            mod = __import__(mod_name)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"  {mod_name}.py — cannot import ({exc})")
            continue
        missing = [n for n in names if not hasattr(mod, n)]
        if missing:
            problems.append(
                f"  {mod_name}.py is OUT OF DATE — missing: {', '.join(missing)}")
    if problems:
        print("\n" + "!" * 68)
        print("STARTUP PROBLEMS — fix these before the demo:")
        print("\n".join(problems))
        print("Missing packages: install them into THIS interpreter with")
        print(f'    "{sys.executable}" -m pip install -r requirements.txt')
        print("Out-of-date modules: replace the WHOLE host/ folder from the")
        print("latest zip so every file is from the same version. Then restart.")
        print("!" * 68 + "\n")
    else:
        print("  module self-check: OK (all host files are the same version)")


if __name__ == "__main__":
    print("CrashGuard call server starting on port", PORT)
    print(f"  DEMO_MODE={DEMO_MODE}  DRY_RUN={DRY_RUN}  MOCK_AI={MOCK_AI}")
    _startup_selfcheck()
    _load_cached_gps()

    # Resolve the fallback location once, now, so that even if the phone never
    # sends live GPS the opening briefing speaks a real street name instead of
    # whatever placeholder text sits in victim_profile.json.
    def _warm_fallback() -> None:
        g = PROFILE.get("gps", {})
        if g.get("lat") is None:
            return
        try:
            from location_services import reverse_geocode
            a = reverse_geocode(g["lat"], g["lon"])
            if a.get("full"):
                PROFILE["gps"]["nearest_address"] = a["full"]
                print(f"  fallback location resolved: {a['full']}")
        except Exception as exc:  # noqa: BLE001
            print(f"  fallback location lookup skipped: {exc}")

    threading.Thread(target=_warm_fallback, daemon=True).start()
    if DRY_RUN:
        print("  (dry run: crash triggers are logged, no phone calls are placed)")
    try:
        import socket
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        ip = _s.getsockname()[0]
        _s.close()
        print(f"  PHONE DASHBOARD -> http://{ip}:{PORT}/  (phone on the same network)")
    except OSError:
        print(f"  PHONE DASHBOARD -> http://<this-laptop-ip>:{PORT}/")
    app.run(host="0.0.0.0", port=PORT)