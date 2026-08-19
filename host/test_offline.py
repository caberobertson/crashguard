"""
test_offline.py — prove the whole server works with ZERO accounts and ZERO cost.

Runs the Flask app in-process (no network, no Twilio, no API key) and walks the
exact webhook sequence Twilio would: trigger -> /voice -> /respond x3 -> hangup.

    cd host
    python test_offline.py

Every check should print PASS.
"""

import os

os.environ["DRY_RUN"] = "1"    # never dial
os.environ["MOCK_AI"] = "1"    # keyword brain, no API key
os.environ["DEMO_MODE"] = "1"

import call_server  # noqa: E402  (import after env is set)

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> None:
    c = call_server.app.test_client()
    print("\n[1] health check")
    r = c.get("/health")
    check("/health returns 200", r.status_code == 200, str(r.status_code))
    check("dry_run active", r.get_json().get("dry_run") is True)

    print("\n[2] crash trigger (as the BLE bridge would send it)")
    r = c.post("/trigger-call", json={"peak_g": 5.2, "source": "ble"})
    check("trigger accepted", r.status_code == 200, r.get_data(as_text=True))
    check("dry-run short-circuit", r.get_json().get("status") == "dry-run")

    r = c.post("/trigger-call", json={"peak_g": 9.9, "source": "ble"})
    check("duplicate suppressed by cooldown", r.status_code == 409)

    print("\n[3] call connects -> /voice speaks the briefing")
    r = c.post("/voice", data={"CallSid": "TEST123"})
    xml = r.get_data(as_text=True)
    check("/voice returns TwiML", r.status_code == 200 and "<Response>" in xml)
    check("briefing includes occupant name", "Jordan Q. Sample" in xml, xml[:200])
    check("briefing includes GPS", "32.2809" in xml)
    check("briefing includes blood type", "O positive" in xml)
    check("speech Gather present", "<Gather" in xml and 'input="speech"' in xml)

    print("\n[4] dispatcher Q&A -> /respond -> /reply")
    qa = [
        ("What is the patient's blood type?", "O positive"),
        ("Where exactly is the vehicle?", "32.2809"),
        ("Any allergies I should know about?", "Penicillin"),
    ]
    for question, expected in qa:
        r = c.post("/respond", data={"CallSid": "TEST123", "SpeechResult": question})
        xml = r.get_data(as_text=True)
        check(f'"{question[:30]}..." hands off to /reply',
              "/reply" in xml or "<Redirect" in xml, xml[:160])
        # poll /reply until the (mock, instant) answer is ready
        answer_xml = ""
        for _ in range(15):
            r2 = c.post("/reply", data={"CallSid": "TEST123"})
            answer_xml = r2.get_data(as_text=True)
            if expected in answer_xml or "<Gather" in answer_xml:
                break
        check(f'answers "{question[:30]}..."', expected in answer_xml, answer_xml[:160])
        check("keeps listening (Gather loop)", "<Gather" in answer_xml)

    print("\n[5] dispatcher closes -> polite hangup")
    r = c.post("/respond", data={"CallSid": "TEST123",
                                 "SpeechResult": "That's all, goodbye."})
    xml = r.get_data(as_text=True)
    check("call ends with <Hangup/>", "<Hangup" in xml, xml[:200])

    print("\n[6] silence handling")
    r = c.post("/voice-reprompt", data={"CallSid": "TEST123"})
    check("first silence -> nudge + Gather",
          "<Gather" in r.get_data(as_text=True))

    print("\n[7] phone dashboard pipeline")
    r = c.get("/")
    html = r.get_data(as_text=True)
    check("dashboard page served", r.status_code == 200 and "CrashGuard" in html)
    check("dashboard has CANCEL button", "CANCEL" in html)

    r = c.post("/telemetry", json={"g": 1.02, "pk": 4.87, "st": 1, "cd": 7})
    check("telemetry accepted, no command pending",
          r.get_json().get("command") == "none")

    r = c.get("/status")
    s = r.get_json()
    check("status reflects board state",
          s.get("st") == 1 and s.get("cd") == 7 and s.get("stale") is False, str(s))

    c.post("/cancel")
    r = c.get("/pending-command")
    check("CANCEL queued for the bridge", r.get_json().get("command") == "CANCEL")
    r = c.get("/pending-command")
    check("command consumed (one-shot)", r.get_json().get("command") == "none")

    c.post("/rearm")
    r = c.post("/telemetry", json={"g": 1.0, "pk": 1.0, "st": 2, "cd": 0})
    check("REARM delivered via telemetry response (Wi-Fi path)",
          r.get_json().get("command") == "REARM")

    print("\n[8] live phone GPS override")
    r = c.post("/set-location", json={"lat": 32.2999, "lon": -106.7600})
    check("location accepted", r.status_code == 200, r.get_data(as_text=True))
    # force a fresh trigger past the cooldown, then confirm the briefing uses it
    call_server.LAST_TRIGGER_TS = 0
    c.post("/trigger-call", json={"peak_g": 5.0, "source": "ble"})
    r = c.post("/voice", data={"CallSid": "GPS1"})
    xml = r.get_data(as_text=True)
    check("briefing speaks the LIVE latitude", "32.2999" in xml, xml[:200])
    r = c.post("/set-location", json={"lat": "bad"})
    check("bad coords rejected", r.status_code == 400)

    print("\n[9] map scene context + documentation flow")
    call_server.SCENE = {
        "address": {"full": "1040 S Horseshoe St, Las Cruces", "city": "Las Cruces",
                    "state": "NM"},
        "cross_streets": ["S Horseshoe St", "Frank Bromilow Mall"],
        "nearby_hospitals": [{"name": "Memorial Medical Center", "distance": "2.1 miles",
                              "direction": "southwest", "distance_m": 3380}],
    }
    call_server.CONV["DOC1"] = []
    call_server.DOC_STATE.pop("DOC1", None)
    call_server.TURN_COUNT["DOC1"] = 0

    def ask(q):
        c.post("/respond", data={"CallSid": "DOC1", "SpeechResult": q})
        for _ in range(15):
            x = c.post("/reply", data={"CallSid": "DOC1"}).get_data(as_text=True)
            if "<Gather" in x or "<Hangup" in x:
                return x
        return ""

    x = ask("What is the nearest cross street?")
    check("answers cross streets from map data", "Horseshoe" in x, x[:160])
    x = ask("Where is the closest hospital?")
    check("answers nearest hospital", "Memorial" in x, x[:160])
    x = ask("What city is this?")
    check("answers city", "Las Cruces" in x, x[:160])
    check("offers to document after a few turns",
          call_server.DOC_STATE.get("DOC1") == "offered"
          or "document" in x.lower(), str(call_server.DOC_STATE.get("DOC1")))

    # say yes -> should ask text or email (no AI call)
    x = c.post("/respond", data={"CallSid": "DOC1", "SpeechResult": "yes please"}
               ).get_data(as_text=True)
    check("yes -> asks how to send", "text message" in x.lower(), x[:160])

    print("\n[10] email flow asks for an address instead of failing")
    import incident_report
    orig = incident_report.smtp_configured
    incident_report.smtp_configured = lambda: True      # pretend SMTP is set up
    try:
        call_server.DOC_STATE["DOC1"] = "awaiting_delivery"
        x = c.post("/respond", data={"CallSid": "DOC1", "SpeechResult": "email"}
                   ).get_data(as_text=True)
        check("bare 'email' -> asks for the address",
              "what email address" in x.lower(), x[:200])
        check("state waits for address",
              call_server.DOC_STATE.get("DOC1") == "awaiting_email")
        x = c.post("/respond", data={"CallSid": "DOC1",
                                     "SpeechResult": "chief at example dot gov"}
                   ).get_data(as_text=True)
        check("spoken address accepted and attempted",
              "example.gov" in x or ("not able" in x.lower() or "could not get that through" in x.lower()), x[:200])
    finally:
        incident_report.smtp_configured = orig

    call_server.DOC_STATE["DOC1"] = "awaiting_delivery"
    x = c.post("/respond", data={"CallSid": "DOC1", "SpeechResult": "email it"}
               ).get_data(as_text=True)
    check("email offered without SMTP -> falls back to text",
          "not configured" in x.lower() or "text message" in x.lower(), x[:200])

    print("\n[11] report request recognised anywhere + spoken number")
    call_server.CONV["N1"] = []
    call_server.DOC_STATE.pop("N1", None)
    x = c.post("/respond", data={"CallSid": "N1",
                                 "SpeechResult": "text me the report"}
               ).get_data(as_text=True)
    check("unprompted 'text me the report' -> asks for number",
          "what number" in x.lower(), x[:200])
    check("state awaits number",
          call_server.DOC_STATE.get("N1") == "awaiting_number")
    x = c.post("/respond", data={
        "CallSid": "N1",
        "SpeechResult": "five seven five five five five zero one four two"}
        ).get_data(as_text=True)
    check("spoken digits accepted, send attempted",
          "+15755550142" in x or ("not able" in x.lower() or "could not get that through" in x.lower()), x[:200])

    call_server.DOC_STATE.pop("N2", None)
    call_server.CONV["N2"] = []
    x = c.post("/respond", data={"CallSid": "N2",
                                 "SpeechResult": "send it to 915 555 1234"}
               ).get_data(as_text=True)
    check("number given inline -> sends straight to it",
          "+19155551234" in x or ("not able" in x.lower() or "could not get that through" in x.lower()), x[:200])

    print(f"\n===== {passed} passed, {failed} failed =====")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
