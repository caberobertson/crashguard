"""
incident_report.py — build and deliver the written incident record.

During the call the AI offers to document everything. If the dispatcher says
yes, this assembles a complete table of known information and sends it by SMS
(Twilio) or email (SMTP), whichever they ask for.

Email needs SMTP settings in .env (Gmail works with an App Password):
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_USER=you@gmail.com
    SMTP_PASS=your_16_char_app_password
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


def build_report(profile: dict, crash: dict, scene: dict | None = None) -> str:
    """Plain-text incident table. Readable in SMS and email alike."""
    scene = scene or {}
    gps = profile.get("gps", {})
    addr = (scene.get("address") or {}).get("full") or gps.get("nearest_address") or "unknown"
    ts = crash.get("detected_at_utc") or datetime.now(timezone.utc).isoformat()

    L = []
    L.append("CRASHGUARD AUTOMATED INCIDENT REPORT")
    L.append("=" * 38)
    L.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    L.append("")
    L.append("-- INCIDENT --")
    L.append(f"Detected      : {ts}")
    L.append(f"Peak impact   : {crash.get('peak_impact_g', 'n/a')} g")
    L.append(f"Alert source  : {crash.get('source', 'n/a')}")
    L.append("")
    L.append("-- LOCATION --")
    L.append(f"Address       : {addr}")
    L.append(f"Coordinates   : {gps.get('lat')}, {gps.get('lon')}")
    a = scene.get("address") or {}
    if a.get("city"):
        L.append(f"City/State    : {a.get('city')}, {a.get('state', '')}".rstrip(", "))
    cs = scene.get("cross_streets") or []
    if cs:
        L.append(f"Cross streets : {', '.join(cs[:4])}")
    hs = scene.get("nearby_hospitals") or []
    if hs:
        L.append("Nearest ER    : " + "; ".join(
            f"{h['name']} ({h['distance']} {h['direction']})" for h in hs[:3]))
    L.append("")
    L.append("-- OCCUPANT --")
    L.append(f"Name          : {profile.get('full_name', 'unknown')}")
    L.append(f"Date of birth : {profile.get('date_of_birth', 'unknown')}")
    L.append(f"Blood type    : {profile.get('blood_type', 'unknown')}")
    L.append(f"Conditions    : {', '.join(profile.get('medical_conditions', [])) or 'none on file'}")
    L.append(f"Allergies     : {', '.join(profile.get('allergies', [])) or 'none on file'}")
    L.append(f"Medications   : {', '.join(profile.get('medications', [])) or 'none on file'}")
    L.append("")
    L.append("-- EMERGENCY CONTACTS --")
    for c in profile.get("emergency_contacts", []) or []:
        L.append(f"  {c.get('name')} ({c.get('relation')}) — {c.get('phone')}")
    if not profile.get("emergency_contacts"):
        L.append("  none on file")
    v = profile.get("vehicle", {})
    if v:
        L.append("")
        L.append("-- VEHICLE --")
        L.append(f"  {v.get('color','')} {v.get('make','')} {v.get('model','')}".strip())
        if v.get("plate"):
            L.append(f"  Plate: {v.get('plate')}")
    L.append("")
    L.append("Generated automatically by CrashGuard (MYOSA 6.0, NMSU).")
    if os.getenv("DEMO_MODE", "1") == "1":
        L.append("*** DEMONSTRATION — NOT A REAL EMERGENCY ***")
    return "\n".join(L)


def send_sms(to_number: str, body: str) -> tuple[bool, str]:
    """Send the report by SMS and CONFIRM it actually left Twilio.

    messages.create() only means Twilio queued it. US local ("10DLC") numbers
    must be registered for A2P messaging or carriers silently drop the traffic
    (error 30034), which looks like success in the API but never arrives. So we
    poll the message status briefly and report the truth."""
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_ = os.getenv("TWILIO_FROM_NUMBER")
    if not all((sid, token, from_, to_number)):
        return False, "Twilio SMS not configured (SID/token/from/to)"
    try:
        import time as _t
        from twilio.rest import Client
        client = Client(sid, token)
        msg = client.messages.create(to=to_number, from_=from_, body=body)

        # Poll for a terminal status. Delivery receipts take a moment.
        status, err = msg.status, None
        for _ in range(8):                       # ~4 s max, off the call path
            _t.sleep(0.5)
            m = client.messages(msg.sid).fetch()
            status, err = m.status, m.error_code
            if status in ("delivered", "sent", "failed", "undelivered"):
                break

        if status in ("delivered", "sent"):
            return True, f"report texted to {to_number} (status={status})"

        hint = ""
        if err in (30034, 30032):
            hint = (" — the Twilio number is not registered for A2P 10DLC "
                    "messaging, so US carriers block its texts. Register at "
                    "Twilio > Messaging > Regulatory Compliance, or use the "
                    "dashboard report instead.")
        elif err == 21610:
            hint = " — that number replied STOP to your Twilio number."
        elif err == 21606:
            hint = " — the From number is not SMS-capable."
        return False, f"SMS not delivered (status={status}, error={err}){hint}"
    except Exception as exc:  # noqa: BLE001
        return False, f"SMS failed: {exc}"


def send_email(to_addr: str, body: str,
               subject: str = "CrashGuard Automated Incident Report") -> tuple[bool, str]:
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pw = os.getenv("SMTP_PASS")
    if not all((host, user, pw, to_addr)):
        return False, "SMTP not configured (SMTP_HOST/USER/PASS) or no address"
    try:
        import smtplib
        from email.message import EmailMessage
        m = EmailMessage()
        m["From"] = user
        m["To"] = to_addr
        m["Subject"] = subject
        m.set_content(body)
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(m)
        return True, f"report emailed to {to_addr}"
    except Exception as exc:  # noqa: BLE001
        return False, f"email failed: {exc}"


# --------------------------------------------------------------------------
# Parsing the dispatcher's spoken answer — no AI call needed (saves credits)
# --------------------------------------------------------------------------
YES = ("yes", "yeah", "yep", "sure", "please", "affirmative", "go ahead",
       "do it", "correct", "ok", "okay")
NO = ("no", "nope", "negative", "not now", "don't", "do not")


def wants_documentation(text: str) -> bool | None:
    """True=yes, False=no, None=unclear."""
    t = " " + text.lower().strip() + " "
    if any(f" {w} " in t or t.startswith(f" {w}") for w in NO):
        return False
    if any(f" {w} " in t or t.startswith(f" {w}") for w in YES):
        return True
    return None


def smtp_configured() -> bool:
    """True only if email can actually be sent — lets the call offer channels
    that will really work instead of promising email and failing."""
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER")
                and os.getenv("SMTP_PASS"))


def normalize_spoken_email(text: str) -> str | None:
    """Speech-to-text renders addresses as words: 'chief at example dot gov'.
    Convert that into chief@example.gov. Returns None if nothing usable."""
    import re
    if "@" in text:                                # already a real address
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
        return m.group(0) if m else None

    t = " " + text.lower().strip() + " "
    # Collapse the spoken symbols WITHOUT destroying word boundaries, so that
    # leading filler ("send it to ...") doesn't get glued onto the address.
    t = t.replace(" at sign ", "@").replace(" at ", "@")
    t = t.replace(" dot ", ".").replace(" period ", ".")
    t = t.replace(" underscore ", "_").replace(" dash ", "-")
    t = t.replace(" hyphen ", "-").replace(" plus ", "+")
    for token in t.split():                        # pick the token holding '@'
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", token)
        if m:
            return m.group(0)
    return None


# Deliberately excludes homophones like "to"/"for"/"ate"/"o": they appear as
# ordinary words far more often than as digits ("send a TO five..." would
# inject a spurious 2).
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "niner": "9",
}


def spoken_digits(text: str) -> str:
    """'five seven five five five five...' -> '5755550142'. Speech-to-text often
    spells numbers out, so a digits-only regex misses them entirely."""
    out = []
    for w in text.lower().replace("-", " ").split():
        w = w.strip(".,!?")
        if w.isdigit():
            out.append(w)
        elif w in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[w])
    return "".join(out)


def extract_phone(text: str) -> str | None:
    """Pull a US phone number out of speech, digits or words."""
    import re
    digits = re.sub(r"\D", "", text)
    if len(digits) not in (10, 11):
        digits = spoken_digits(text)
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) > 11:          # stray digits from surrounding speech
        return "+1" + digits[-10:]
    return None


def wants_report(text: str) -> bool:
    """True if the dispatcher is asking for the written record — recognised
    ANYWHERE in the call, not only inside the scripted offer, so 'send the
    table' or 'text me the details' works whenever they say it."""
    t = text.lower()
    verbs = ("send", "text", "email", "e-mail", "forward", "share", "give me",
             "write", "document", "transmit", "push", "deliver")
    nouns = ("report", "information", "info", "details", "document", "record",
             "records", "everything", "data", "summary", "table", "chart",
             "list", "writeup", "write-up", "documentation", "rundown",
             "it", "that", "this", "them", "over")
    if any(p in t for p in ("text me", "email me", "send me", "send it",
                            "send that", "send the", "send this")):
        return True
    if not any(v in t for v in verbs):
        return False
    return any(n in t for n in nouns)


# Phrases that mean the model wrongly claimed it cannot deliver a report.
# Used as a safety net: the system CAN send, so such an answer is overridden.
_REFUSAL_MARKERS = (
    "cannot send", "can't send", "can not send", "unable to send",
    "don't have the ability to send", "do not have the ability to send",
    "cannot text", "can't text", "cannot email", "can't email",
    "only speak", "only communicate verbally", "verbally", "no capability",
    "not able to send", "don't have capabilities", "do not have capabilities",
    "no ability to send",
)


def is_send_refusal(reply: str) -> bool:
    """True if the model claimed it cannot deliver the written report."""
    r = reply.lower()
    return any(m in r for m in _REFUSAL_MARKERS)


def parse_delivery(text: str) -> tuple[str | None, str | None]:
    """Work out 'text' vs 'email' and pull a number/address if spoken.
    Returns (channel, destination-or-None)."""
    import re
    t = text.lower()
    channel = None
    if any(w in t for w in ("email", "e-mail", "mail it", "send it to my email")):
        channel = "email"
    elif any(w in t for w in ("text", "sms", "message", "txt")):
        channel = "sms"

    dest = None
    spoken = normalize_spoken_email(text)
    if spoken:
        dest = spoken
        channel = channel or "email"
    else:
        phone = extract_phone(text)
        if phone:
            dest = phone
            channel = channel or "sms"
    return channel, dest