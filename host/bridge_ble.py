"""
bridge_ble.py — the laptop's BLE <-> HTTP relay (stands in for the occupant's phone).

Three jobs, all at once:
  1. Crash alerts:  board EVT notify  ->  POST /trigger-call   (places the AI call)
  2. Live status:   board TLM notify  ->  POST /telemetry      (feeds the phone
                                                                dashboard at
                                                                http://<laptop>:5000/)
  3. Phone commands: GET /pending-command every 250 ms -> write CANCEL/REARMj
                     to the board's CMD characteristic (the dashboard's big
                     red button works because of this path).

Run alongside the server:
    python call_server.py     # terminal 1
    python bridge_ble.py      # terminal 2

Options:
    python bridge_ble.py --server http://127.0.0.1:5000 --name MYOSA-CrashGuard
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import requests
from bleak import BleakClient, BleakScanner

SVC_UUID = "b10e0001-c0de-4c9a-8a7e-3b2f1d4e5a6c"
EVT_UUID = "b10e0002-c0de-4c9a-8a7e-3b2f1d4e5a6c"   # crash alerts (notify)
TLM_UUID = "b10e0003-c0de-4c9a-8a7e-3b2f1d4e5a6c"   # live status (notify)
CMD_UUID = "b10e0004-c0de-4c9a-8a7e-3b2f1d4e5a6c"   # commands (write)

CMD_POLL_S = 0.25          # dashboard-cancel latency bound
PING_S = 3.0               # heartbeat so the board knows we're alive
last_alert_uptime = -1_000_000


def _post(url: str, **kw) -> requests.Response:
    return requests.post(url, **kw)


def handle_alert(server: str, payload: bytes) -> None:
    global last_alert_uptime
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        print(f"[bridge] unparseable alert: {payload!r}")
        return
    if data.get("evt") != "crash":
        return

    up = int(data.get("uptime_ms", 0))
    if abs(up - last_alert_uptime) < 20_000:        # firmware re-notifies 5x
        return
    last_alert_uptime = up

    print(f"[bridge] CRASH received {data} -> POST /trigger-call")

    def _send() -> None:
        try:
            r = _post(f"{server}/trigger-call",
                      json={"peak_g": data.get("peak_g"),
                            "decel_g": data.get("decel_g"),
                            "axis": data.get("axis"),
                            "saturated": data.get("saturated"),
                            "source": "ble"},
                      timeout=20)
            print(f"[bridge] server: {r.status_code} {r.text[:160]}")
        except requests.RequestException as exc:
            print(f"[bridge] ERROR reaching server: {exc}")

    # run off the BLE event loop so a slow dial never stalls notifications
    asyncio.get_running_loop().run_in_executor(None, _send)


def handle_status(server: str, payload: bytes) -> None:
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return
    def _send() -> None:
        try:                                        # fire-and-forget, tiny+local
            _post(f"{server}/telemetry", json=data, timeout=1)
        except requests.RequestException:
            pass                                    # dashboard shows "offline"

    asyncio.get_running_loop().run_in_executor(None, _send)


async def pump_commands(server: str, client: BleakClient) -> None:
    """Relay dashboard CANCEL/REARM presses down to the board, and send a
    periodic heartbeat so the board can tell we are still alive. Without the
    heartbeat, killing this program leaves the board thinking a client is
    still attached — it would then refuse the next bridge until power-cycled."""
    loop = asyncio.get_running_loop()
    last_ping = 0.0
    while client.is_connected:
        try:
            r = await loop.run_in_executor(
                None, lambda: requests.get(f"{server}/pending-command", timeout=1))
            cmd = r.json().get("command", "none")
            if cmd in ("CANCEL", "REARM"):
                print(f"[bridge] phone -> board: {cmd}")
                await client.write_gatt_char(CMD_UUID, cmd.encode(), response=True)
        except requests.RequestException:
            pass
        except Exception as exc:
            print(f"[bridge] command write failed: {exc}")

        now = time.monotonic()
        if now - last_ping >= PING_S:
            last_ping = now
            try:
                await client.write_gatt_char(CMD_UUID, b"PING", response=False)
            except Exception:
                break          # link is gone; fall out and reconnect
        await asyncio.sleep(CMD_POLL_S)


async def run(server: str, name: str) -> None:
    print(f"[bridge] scanning for '{name}' …")
    while True:
        device = await BleakScanner.find_device_by_name(name, timeout=8.0)
        if device is None:
            device = next(
                (d for d, adv in (await BleakScanner.discover(
                    timeout=8.0, return_adv=True)).values()
                 if SVC_UUID in (adv.service_uuids or [])),
                None,
            )
        if device is None:
            print("[bridge] not found — is the board powered? retrying …")
            await asyncio.sleep(2)
            continue

        print(f"[bridge] connecting to {device.address} …")
        client = None
        try:
            async with BleakClient(device) as client:
                await client.start_notify(
                    EVT_UUID, lambda _, d: handle_alert(server, bytes(d)))
                await client.start_notify(
                    TLM_UUID, lambda _, d: handle_status(server, bytes(d)))
                print("[bridge] connected — relaying status, waiting for crash events")
                print(f"[bridge] open the dashboard on your phone: {server}/ "
                      "(use the laptop's LAN IP, not 127.0.0.1)")
                await pump_commands(server, client)   # returns on disconnect
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[bridge] connection lost: {exc}")
        finally:
            # Always hand the link back cleanly so the board re-advertises
            # immediately instead of waiting out the heartbeat timeout.
            if client is not None:
                try:
                    if client.is_connected:
                        await client.disconnect()
                        print("[bridge] disconnected cleanly")
                except Exception:
                    pass
        print("[bridge] reconnecting …")
        await asyncio.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="CrashGuard BLE bridge")
    ap.add_argument("--server", default="http://127.0.0.1:5000")
    ap.add_argument("--name", default="MYOSA-CrashGuard")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.server.rstrip("/"), args.name))
    except KeyboardInterrupt:
        # the finally-block in run() releases the BLE link on the way out
        print("\n[bridge] bye")


if __name__ == "__main__":
    main()
