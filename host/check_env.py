"""
check_env.py — figure out why the server says your Twilio/Claude vars are missing.

Run it from the SAME folder and the SAME way you run call_server.py:
    python check_env.py
"""

import os
from pathlib import Path

REQUIRED = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "EMERGENCY_CONTACT_NUMBER",
    "PUBLIC_BASE_URL",
    "ANTHROPIC_API_KEY",
]

print("=" * 60)
print("1. WHERE AM I RUNNING FROM?")
cwd = Path.cwd()
print(f"   current directory : {cwd}")
print(f"   this script lives : {Path(__file__).resolve().parent}")
if cwd != Path(__file__).resolve().parent:
    print("   !! You are NOT running from the script's folder. python-dotenv")
    print("      looks in the CURRENT directory — cd into the host folder.")

print("\n2. IS THERE A .env HERE?")
files = sorted(p.name for p in cwd.iterdir() if p.name.lower().startswith(".env"))
if not files:
    print("   !! No .env* file found in this directory. That is the problem.")
else:
    for f in files:
        size = (cwd / f).stat().st_size
        flag = ""
        if f != ".env":
            flag = "  <-- WRONG NAME (must be exactly '.env')"
        print(f"   {f}  ({size} bytes){flag}")

print("\n3. WHAT DOES python-dotenv ACTUALLY PARSE?")
try:
    from dotenv import dotenv_values, load_dotenv
except ImportError:
    print("   !! python-dotenv is not installed in THIS interpreter.")
    raise SystemExit(1)

env_path = cwd / ".env"
if env_path.exists():
    parsed = dotenv_values(env_path)
    print(f"   {len(parsed)} key(s) parsed from {env_path}")
    unparsed = []
    for i, line in enumerate(env_path.read_text(encoding="utf-8",
                                                errors="replace").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            unparsed.append((i, line))
    if unparsed:
        print("   !! These lines have no '=' and are IGNORED:")
        for i, line in unparsed:
            print(f"      line {i}: {line[:60]}")
else:
    parsed = {}
    print("   no .env in this directory to parse")

print("\n4. REQUIRED VALUES (after load_dotenv, same as the server does)")
load_dotenv()
ok = True
for k in REQUIRED:
    v = os.getenv(k, "")
    if not v:
        print(f"   MISSING  {k}")
        ok = False
    elif v.startswith("PUT_YOUR") or "xxxx" in v.lower() or "FILL IN" in v:
        print(f"   PLACEHOLDER  {k} = {v[:28]}...")
        ok = False
    elif v != v.strip():
        print(f"   WHITESPACE  {k} has leading/trailing spaces -> {v!r}")
        ok = False
    else:
        shown = v if k in ("TWILIO_FROM_NUMBER", "EMERGENCY_CONTACT_NUMBER",
                           "PUBLIC_BASE_URL") else v[:10] + "..."
        print(f"   OK       {k} = {shown}")

print("\n" + "=" * 60)
print("ALL GOOD — start the server." if ok else
      "FIX THE ITEMS ABOVE, then re-run this check.")
