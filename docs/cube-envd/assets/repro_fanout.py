#!/usr/bin/env python3
"""What does Go envd do when one subscriber of a process is slow?

Code reading (MultiplexedChannel):
  * Fork() hands each attachment an *unbuffered* channel
  * the multiplexer goroutine does a blocking `cons <- v` per subscriber
  => the slowest subscriber should throttle every other subscriber and the child.

This measures it on the live stack:
  1. A = Start stream for `timeout 12 yes ...`, read slowly (8 KiB / 150 ms)
  2. B = Connect(pid) stream on the same process, read as fast as possible
     -> does B see data at B's own pace, or is it pinned to A's pace?
  3. abort A's connection, then check whether B keeps receiving and whether the
     sandbox still runs new commands (i.e. did the fan-out wedge).

Usage: repro_fanout.py --label stock --template tpl-xxx --mode fanout|abort
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


def read_frames(resp, on_event, slow=None, stop=None):
    """Parse Connect envelopes out of a streaming response."""
    buffer = bytearray()
    for chunk in resp.raw.stream(amt=8192, decode_content=False):
        if stop is not None and stop.is_set():
            return
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
            if event.get("end") is not None:
                on_event("end", event["end"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--api-url", default="http://127.0.0.1:3000")
    args = ap.parse_args()

    config = Config(api_url=args.api_url, template_id=args.template)
    sandbox = Sandbox.create(timeout=900, config=config)
    host = sandbox.get_host(ENVD_PORT)
    out: dict = {"label": args.label, "sandbox_id": sandbox.sandbox_id}
    try:
        # ---- A: Start stream, read slowly ---------------------------------
        pid_box: dict = {}
        a_bytes = [0]
        a_state: dict = {}

        def a_event(kind, payload):
            if kind == "start":
                pid_box["pid"] = payload.get("pid")
            elif kind == "stdout":
                a_bytes[0] += payload
            elif kind == "end_stream":
                a_state["end_stream"] = payload.decode("utf-8", "replace")[:160]

        resp_a = requests.post(
            f"http://{host}/process.Process/Start",
            data=envelope({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", "timeout 12 yes abcdefghijklmnopqrstuvwxyz0123456789"], "envs": {}}, "stdin": False}),
            headers=HEADERS, stream=True, timeout=120)
        t_a = threading.Thread(target=read_frames, args=(resp_a, a_event), kwargs={"slow": 0.15}, daemon=True)
        t_a.start()

        # wait for the start event to learn the pid
        deadline = time.monotonic() + 5
        while "pid" not in pid_box and time.monotonic() < deadline:
            time.sleep(0.05)
        out["pid"] = pid_box.get("pid")

        # ---- B: Connect(pid), read fast -----------------------------------
        b_bytes = [0]
        b_buckets: dict[int, int] = {}
        b_end: dict = {}
        t0 = time.monotonic()

        def b_event(kind, payload):
            if kind == "stdout":
                b_bytes[0] += payload
                b_buckets[int(time.monotonic() - t0)] = b_bytes[0]
            elif kind == "end_stream":
                b_end["end_stream"] = payload.decode("utf-8", "replace")[:160]

        resp_b = requests.post(
            f"http://{host}/process.Process/Connect",
            data=envelope({"process": {"pid": pid_box["pid"]}}),
            headers=HEADERS, stream=True, timeout=120)
        t_b = threading.Thread(target=read_frames, args=(resp_b, b_event), daemon=True)
        t_b.start()

        time.sleep(4.0)
        out["during_4s"] = {
            "a_bytes": a_bytes[0],
            "a_rate_kib_s": round(a_bytes[0] / 4 / 1024, 1),
            "b_bytes": b_bytes[0],
            "b_rate_kib_s": round(b_bytes[0] / 4 / 1024, 1),
            "b_cumulative_by_second": b_buckets,
            "b_end": b_end.get("end_stream"),
        }

        # ---- abort A abruptly, then see whether B keeps flowing -----------
        try:
            resp_a.close()
        except Exception:  # noqa: BLE001
            pass
        before = b_bytes[0]
        time.sleep(3.0)
        after_abort = b_bytes[0]
        out["after_abort_3s"] = {
            "b_bytes_gained": after_abort - before,
            "b_end_stream": b_end.get("end_stream"),
            "a_bytes_after_abort": a_bytes[0],
        }

        # is the sandbox still able to run a *new* command?
        t1 = time.monotonic()
        try:
            res = sandbox.commands.run("echo NEW-CMD-OK")
            out["new_command"] = {"stdout": res.stdout.strip(), "seconds": round(time.monotonic() - t1, 2)}
        except Exception as exc:  # noqa: BLE001
            out["new_command"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}",
                                  "seconds": round(time.monotonic() - t1, 2)}

        # and can we still attach to the still-running process?
        try:
            resp_c = requests.post(
                f"http://{host}/process.Process/Connect",
                data=envelope({"process": {"pid": pid_box["pid"]}}),
                headers=HEADERS, stream=True, timeout=30)
            got = [0]

            def c_event(kind, payload):
                if kind == "stdout":
                    got[0] += payload

            t_c = threading.Thread(target=read_frames, args=(resp_c, c_event), daemon=True)
            t_c.start()
            time.sleep(2.0)
            out["reattach"] = {"bytes_in_2s": got[0]}
            resp_c.close()
        except Exception as exc:  # noqa: BLE001
            out["reattach"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        try:
            sandbox.kill()
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
