"""
simulate_crash.py — fire a fake crash at the call server, no hardware needed.

Use it to test the whole phone-call pipeline from your laptop:
    python simulate_crash.py                       # default 5.2 g
    python simulate_crash.py --peak-g 7.8
    python simulate_crash.py --server http://127.0.0.1:5000
"""

import argparse

import requests


def main() -> None:
    ap = argparse.ArgumentParser(description="Send a simulated crash trigger")
    ap.add_argument("--server", default="http://127.0.0.1:5000")
    ap.add_argument("--peak-g", type=float, default=5.2)
    args = ap.parse_args()

    url = f"{args.server.rstrip('/')}/trigger-call"
    body = {"peak_g": args.peak_g, "source": "simulated"}
    print(f"POST {url}  {body}")
    r = requests.post(url, json=body, timeout=15)
    print(f"-> {r.status_code}: {r.text}")


if __name__ == "__main__":
    main()
