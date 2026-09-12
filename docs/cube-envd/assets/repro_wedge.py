#!/usr/bin/env python3
"""Does a slow-then-disconnected subscriber wedge Go's fan-out for that process?

Uses an unbounded producer (`yes`, no timeout) so "no data" cannot be explained
by the process having exited:
  1. A = Start(`yes`), read slowly (8 KiB / 150 ms)
  2. after 2.5 s abort A's connection
  3. after 3.5 s attach C = Connect(pid), read fast, count bytes for 3 s
  4. check the child is still alive from a *separate* command

Usage: repro_wedge.py --label stock --template tpl-xxx
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import threading
import time

sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")

import requests  # noqa: E402
from cubesandbox import Config, Sandbox  # noqa: E402

ENVD_PORT = 49983
HEADERS = {
    "Content-Type": "application/connect+json",
    "Connect-Protocol-Version": "1",
    "Connect-Content-Encoding": "identity",
}


def envelope(payload: dict) -> bytes:
    data = json.dumps(payload).encode()
    return bytes([0]) + struct.pack(">I", len(data)) + data


def read_frames(resp, on_event, slow=None):
    buffer = bytearray()
    try:
        for chunk in resp.raw.stream(amt=8192, decode_content=False):
            buffer.extend(chunk)
            if slow:
                time.sleep(slow)
            while len(buffer) >= 5:
                flags = buffer[0]
                size = struct.unpack(">I", buffer[1:5])[0]
                if len(buffer) < 5 + size:
                    break
                payload = bytes(buffer[5:5 + size])
                del buffer[:5 + size]
                if flags & 0x02:
                    on_event("end_stream", payload)
                    continue
                try:
                    event = json.loads(payload.decode()).get("event") or {}
                except Exception:  # noqa: BLE001
                    continue
                data = event.get("data") or {}
                if data.get("stdout"):
                    on_event("stdout", len(data["stdout"]))
                if event.get("start"):
                    on_event("start", event["start"])
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--template", required=True)
    args = ap.parse_args()

    config = Config(api_url="http://127.0.0.1:3000", template_id=args.template)
    sandbox = Sandbox.create(timeout=900, config=config)
    host = sandbox.get_host(ENVD_PORT)
    out: dict = {"label": args.label, "sandbox_id": sandbox.sandbox_id}
    try:
        pid_box: dict = {}
        a_bytes = [0]

        def a_event(kind, payload):
            if kind == "start":
                pid_box["pid"] = payload.get("pid")
            elif kind == "stdout":
                a_bytes[0] += payload

        resp_a = requests.post(
            f"http://{host}/process.Process/Start",
            data=envelope({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", "yes abcdefghijklmnopqrstuvwxyz0123456789"], "envs": {}}, "stdin": False}),
            headers=HEADERS, stream=True, timeout=120)
        threading.Thread(target=read_frames, args=(resp_a, a_event), kwargs={"slow": 0.15}, daemon=True).start()
        deadline = time.monotonic() + 5
        while "pid" not in pid_box and time.monotonic() < deadline:
            time.sleep(0.05)
        out["pid"] = pid_box.get("pid")
        time.sleep(2.5)
        out["a_bytes_before_abort"] = a_bytes[0]

        try:
            resp_a.close()
        except Exception:  # noqa: BLE001
            pass
        out["aborted_a"] = True
        time.sleep(3.5)

        # is the child still there / producing?
        out["child_state"] = sandbox.commands.run("pgrep -c -f 'yes abcdef' || true").stdout.strip()
        out["a_bytes_frozen_at"] = a_bytes[0]

        # attach a fresh fast consumer
        c_bytes = [0]
        c_events = []

        def c_event(kind, payload):
            if kind == "stdout":
                c_bytes[0] += payload
                c_events.append(("stdout", payload))
            elif kind == "end_stream":
                c_events.append(("end_stream", payload.decode("utf-8", "replace")[:160]))
            elif kind == "start":
                c_events.append(("start", payload))

        try:
            resp_c = requests.post(
                f"http://{host}/process.Process/Connect",
                data=envelope({"process": {"pid": pid_box["pid"]}}),
                headers=HEADERS, stream=True, timeout=20)
            threading.Thread(target=read_frames, args=(resp_c, c_event), daemon=True).start()
            time.sleep(3.0)
            out["reattach"] = {"bytes_in_3s": c_bytes[0], "events": c_events[:5]}
            resp_c.close()
        except Exception as exc:  # noqa: BLE001
            out["reattach"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}

        # clean up the runaway producer
        sandbox.commands.run("pkill -9 yes || true")
    finally:
        try:
            sandbox.kill()
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
