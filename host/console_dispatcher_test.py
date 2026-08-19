"""
console_dispatcher_test.py — rehearse the dispatcher conversation in a terminal.

You type what the 911 dispatcher would say; CrashGuard answers exactly as it
would on the phone (same prompt, same data), minus Twilio. Perfect for tuning
victim_profile.json and rehearsing the conference Q&A.

    python console_dispatcher_test.py --mock          # no API key needed
    python console_dispatcher_test.py                 # live Claude answers
    python console_dispatcher_test.py --peak-g 6.1

Type 'quit' to exit.
"""

import argparse

from dotenv import load_dotenv

from dispatcher_agent import (
    agent_reply,
    build_intro,
    default_crash_context,
    load_profile,
)


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="CrashGuard console rehearsal")
    ap.add_argument("--mock", action="store_true",
                    help="use the offline keyword brain (no API key, no cost)")
    ap.add_argument("--peak-g", type=float, default=5.4)
    ap.add_argument("--profile", default=None, help="path to victim_profile.json")
    args = ap.parse_args()

    profile = load_profile(args.profile)
    crash = default_crash_context(peak_g=args.peak_g, source="console")
    history: list[dict] = []

    intro = build_intro(profile, crash, demo_mode=True)
    print("\n=== CALL CONNECTED ===")
    print(f"CrashGuard: {intro}\n")
    history.append({"role": "user", "content": "[call connected]"})
    history.append({"role": "assistant", "content": intro})

    while True:
        try:
            line = input("Dispatcher: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.lower() in {"quit", "exit"}:
            break

        history.append({"role": "user", "content": line})
        answer = agent_reply(history, profile, crash, demo_mode=True, mock=args.mock)
        history.append({"role": "assistant", "content": answer})
        print(f"CrashGuard: {answer}\n")

    print("=== CALL ENDED ===")


if __name__ == "__main__":
    main()
