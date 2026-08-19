"""
dispatcher_agent.py — the "voice" of CrashGuard.

Builds the emergency-call script and answers a dispatcher's questions using
the Claude API, grounded ONLY in the occupant profile (victim_profile.json)
and the live crash data. A --mock mode answers from simple keyword rules so
the whole pipeline can be tested with zero API keys and zero cost.

Used by:  call_server.py  (live Twilio calls)
          console_dispatcher_test.py  (keyboard-only rehearsal)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# Current model names/pricing: https://docs.claude.com/en/api/overview
# Haiku is the low-latency choice for short, grounded dispatcher replies.
# Override with CLAUDE_MODEL in .env (e.g. a Sonnet model) if you want richer
# phrasing and don't mind ~2x the response time.
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")


# --------------------------------------------------------------------------
# Profile & crash context
# --------------------------------------------------------------------------
def load_profile(path: str | None = None) -> dict:
    path = path or os.getenv("VICTIM_PROFILE", "victim_profile.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def default_crash_context(peak_g: float | None = None, source: str = "unknown",
                          decel_g: float | None = None, axis: str | None = None,
                          saturated: bool | None = None) -> dict:
    ctx = {
        "peak_impact_g": peak_g,
        "detected_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,  # "ble" | "wifi" | "simulated"
    }
    if decel_g is not None and axis:
        # negative = deceleration (the vehicle was slowed, i.e. it hit something
        # ahead of it); positive = it was accelerated (struck from behind/side)
        kind = "deceleration" if decel_g < 0 else "acceleration"
        ctx["impact_axis"] = axis
        ctx["impact_g_along_axis"] = round(decel_g, 2)
        ctx["impact_type"] = f"{abs(decel_g):.1f} g {kind} along the {axis} axis"
    if saturated:
        ctx["note"] = ("Impact exceeded the sensor's 16 g measurement range — "
                       "actual severity is higher than the recorded peak.")
    return ctx


# --------------------------------------------------------------------------
# Spoken opening statement (played as soon as the call connects)
# --------------------------------------------------------------------------
def build_intro(profile: dict, crash: dict, demo_mode: bool) -> str:
    gps = profile.get("gps", {})
    contacts = profile.get("emergency_contacts", [])
    conditions = ", ".join(profile.get("medical_conditions", [])) or "none on file"

    parts = []
    if demo_mode:
        parts.append(
            "This is a demonstration of the CrashGuard automated emergency system. "
            "No real emergency is in progress."
        )
    parts.append(
        "This is an automated emergency call from CrashGuard, an in-vehicle "
        "crash detection system. A vehicle collision has been detected and the "
        "occupant has not responded to a cancellation prompt."
    )
    parts.append(f"The registered occupant is {profile.get('full_name', 'unknown')}.")
    if gps:
        addr = gps.get("nearest_address")
        lat, lon = gps.get("lat"), gps.get("lon")
        if addr:
            parts.append(
                f"The vehicle's location is {addr}. "
                f"GPS coordinates: latitude {lat}, longitude {lon}."
            )
        elif lat is not None and lon is not None:
            parts.append(
                f"The vehicle's GPS location is latitude {lat}, longitude {lon}."
            )
    if crash.get("peak_impact_g"):
        parts.append(f"The recorded peak impact was {crash['peak_impact_g']:.1f} g.")
    parts.append(
        f"Blood type {profile.get('blood_type', 'unknown')}. "
        f"Known medical conditions: {conditions}."
    )
    if contacts:
        c = contacts[0]
        parts.append(
            f"The primary emergency contact is {c.get('name')}, {c.get('relation')}, "
            f"phone {c.get('phone')}."
        )
    parts.append(
        "The occupant may be unresponsive. I can answer questions about the "
        "occupant or the crash. Go ahead when ready."
    )
    return " ".join(parts)


# --------------------------------------------------------------------------
# System prompt for the conversational agent
# --------------------------------------------------------------------------
def build_system_prompt(profile: dict, crash: dict, demo_mode: bool,
                        scene: dict | None = None) -> str:
    """Compact prompt. Kept deliberately short — every token here is billed on
    EVERY turn of the call, so brevity directly cuts cost and latency."""
    demo_line = ("If asked whether this is real, say clearly it is a DEMONSTRATION."
                 if demo_mode else "")

    scene_block = ""
    if scene:
        try:
            from location_services import context_summary
            s = context_summary(scene)
            if s:
                scene_block = f"\nSCENE (map data — use for cross streets, city, hospitals):\n{s}\n"
        except Exception:
            pass

    impact_line = ""
    if crash.get("impact_type"):
        impact_line = f", {crash['impact_type']}"
    if crash.get("note"):
        impact_line += f". {crash['note']}"

    return f"""You are CrashGuard, an automated in-vehicle emergency system on a live phone
call with a dispatcher, speaking for a crash victim who may be unconscious.

RULES
- Spoken sentences only. No markdown or lists. 1-2 short sentences per reply.
- Read phone numbers and coordinates digit by digit when asked.
- Answer from the data below. If a fact is not here, say you do not have it.
  Never invent injuries; you have no camera or microphone in the cabin.
- Open questions: give location first, then identity, then medical, then impact.
- {demo_line}

WHAT YOU CAN DO (never say you are unable to do these)
- Send the full written incident report by text message, to ANY number the
  dispatcher gives you, or by email. If they ask for it, confirm briefly and
  ask only for whatever is missing (the number or the address). The system
  performs the sending; you simply acknowledge it.
- Answer questions about the surroundings from the SCENE data: nearest cross
  streets, city and county, and nearby hospitals with distance and direction.

REASONING
- Infer what the dispatcher actually needs. "Where do I send units?" wants the
  address and cross streets, not coordinates. "How bad is it?" wants the impact
  severity and what that implies about likely injury risk, stated cautiously.
- Volunteer the single most useful next fact rather than waiting to be asked,
  but keep it to one short sentence.

NEVER CONTRADICT YOURSELF
- Either you have a fact or you do not. Do not say you have map data and then
  say you lack the detail in the same answer. If SCENE marks something NOT
  AVAILABLE, simply say you do not have it and offer what you do have.
- Do not repeat the address twice in one reply, and do not pad with hedges.

CRASH: peak {crash.get('peak_impact_g', 'n/a')} g, detected {crash.get('detected_at_utc', 'n/a')}, source {crash.get('source', 'n/a')}{impact_line}
{scene_block}
OCCUPANT:
{json.dumps(profile, separators=(',', ':'))}
"""


# --------------------------------------------------------------------------
# Reply generation
# --------------------------------------------------------------------------
def agent_reply(
    history: list[dict],
    profile: dict,
    crash: dict,
    demo_mode: bool = True,
    mock: bool = False,
    model: str = DEFAULT_MODEL,
    scene: dict | None = None,
) -> str:
    """history = [{"role": "user"|"assistant", "content": str}, ...] ending with the
    dispatcher's newest utterance as a "user" message."""
    if mock or os.getenv("MOCK_AI") == "1":
        return _mock_reply(history[-1]["content"], profile, crash, scene)

    from anthropic import Anthropic  # deferred so mock mode needs no SDK/key

    # Make sure .env is loaded even if the caller forgot (e.g. run directly
    # from an IDE). Harmless if it was already loaded.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key or api_key.startswith("sk-ant-xxx"):
        raise SystemExit(
            "\nANTHROPIC_API_KEY is not set.\n"
            "  1. Copy .env.example to .env  (in the host/ folder)\n"
            "  2. Put your real key on the ANTHROPIC_API_KEY line (no quotes, no spaces)\n"
            "  3. Make sure the file is named exactly .env (not .env.txt)\n"
            "Or rehearse with no key at all:  python console_dispatcher_test.py --mock\n"
        )

    client = Anthropic(api_key=api_key)

    # Credit efficiency:
    #  * only the last few turns are sent — a dispatcher call rarely needs more
    #    context than that, and history is billed on every single turn.
    #  * the system prompt is marked for prompt caching, so the (unchanging)
    #    profile + scene data is billed at the cheap cached rate after turn 1.
    turns = int(os.getenv("HISTORY_TURNS", "6"))
    trimmed = history[-turns:] if len(history) > turns else history

    msg = client.messages.create(
        model=model,
        max_tokens=100,
        system=[{
            "type": "text",
            "text": build_system_prompt(profile, crash, demo_mode, scene),
            "cache_control": {"type": "ephemeral"},
        }],
        messages=trimmed,
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


# --------------------------------------------------------------------------
# Offline mock brain — keyword lookup so the demo pipeline can be tested
# without an API key (set MOCK_AI=1 or pass mock=True).
# --------------------------------------------------------------------------
def _mock_reply(text: str, profile: dict, crash: dict, scene: dict | None = None) -> str:
    t = text.lower()
    gps = profile.get("gps", {})
    scene = scene or {}

    # map-derived answers first (these need no AI in real mode either)
    if any(k in t for k in ("cross street", "cross-street", "intersection", "nearest street")):
        cs = scene.get("cross_streets") or []
        return ("Nearby streets are " + ", ".join(cs[:3]) + ".") if cs \
            else "I do not have cross street information."
    if any(k in t for k in ("hospital", "emergency room", "trauma", "nearest er")):
        hs = scene.get("nearby_hospitals") or []
        if hs:
            h = hs[0]
            return (f"The nearest hospital is {h['name']}, about {h['distance']} "
                    f"to the {h['direction']}.")
        return "I do not have hospital information for this area."
    if any(k in t for k in ("landmark", "near you", "what's around", "whats around",
                            "anything nearby", "notable", "businesses", "buildings")):
        lm = scene.get("landmarks") or []
        if lm:
            m = lm[0]
            return (f"Nearby is {m['name']}, about {m['distance']} to the "
                    f"{m['direction']}.")
        return "I do not have landmark information for this area."
    if any(k in t for k in ("zip", "postal", "postcode")):
        a = scene.get("address") or {}
        if a.get("postcode"):
            return f"The ZIP code is {a['postcode']}."
        return "I do not have the ZIP code."
    if any(k in t for k in ("what city", "which city", "what town", "county")):
        a = scene.get("address") or {}
        if a.get("city"):
            return f"The vehicle is in {a['city']}, {a.get('state', '')}".strip().rstrip(",") + "."
        return "I do not have city information."

    if any(k in t for k in ("blood", "type")):
        return f"The occupant's blood type is {profile.get('blood_type', 'not on file')}."
    if any(k in t for k in ("where", "location", "address", "gps", "coordinates")):
        return (
            f"The vehicle is near {gps.get('nearest_address', 'an unknown address')}. "
            f"GPS latitude {gps.get('lat')}, longitude {gps.get('lon')}."
        )
    if any(k in t for k in ("name", "who is", "identity", "occupant")):
        return f"The registered occupant is {profile.get('full_name', 'unknown')}."
    if "allerg" in t:
        allergies = ", ".join(profile.get("allergies", [])) or "none on file"
        return f"Known allergies: {allergies}."
    if any(k in t for k in ("condition", "medical", "history", "medication", "meds")):
        conditions = ", ".join(profile.get("medical_conditions", [])) or "none on file"
        meds = ", ".join(profile.get("medications", [])) or "none on file"
        return f"Known conditions: {conditions}. Current medications: {meds}."
    if any(k in t for k in ("contact", "family", "next of kin")):
        cs = profile.get("emergency_contacts", [])
        if cs:
            c = cs[0]
            return f"Primary contact is {c.get('name')}, {c.get('relation')}, phone {c.get('phone')}."
        return "No emergency contacts are on file."
    if any(k in t for k in ("impact", "severe", "how bad", "speed", "force", "g force", "hard")):
        pg = crash.get("peak_impact_g")
        return (
            f"The recorded peak impact was {pg:.1f} g."
            if pg
            else "Impact magnitude was not recorded."
        )
    if any(k in t for k in ("vehicle", "car", "make", "model", "plate", "color")):
        v = profile.get("vehicle", {})
        return (
            f"The vehicle is a {v.get('color', '')} {v.get('make', '')} "
            f"{v.get('model', '')}, plate {v.get('plate', 'unknown')}."
        ).replace("  ", " ")
    if any(k in t for k in ("real", "drill", "test", "demo")):
        return "This is a demonstration of the CrashGuard system. No real emergency exists."
    if any(k in t for k in ("awake", "conscious", "responsive", "breathing", "injur")):
        return (
            "I cannot sense the occupant's condition directly. They did not respond "
            "to the ten second cancellation prompt after the impact."
        )
    return (
        "I did not catch that. I can provide the occupant's identity, location, "
        "blood type, medical history, or crash details."
    )