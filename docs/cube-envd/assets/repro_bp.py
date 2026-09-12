#!/usr/bin/env python3
"""Characterise the slow-consumer policy: how much a user actually loses.

Cases (per template):
  fast_drain   read the socket and count bytes only (upper bound on the path)
  sdk_like     parse every Connect envelope + base64 decode (what the SDKs do)
  cat_burst    cat a 50 MiB file with an sdk_like consumer (realistic burst)
  pty_flood    spam a PTY for 1.5 s, then check the session still answers

Usage: repro_bp.py --label pr27 --template tpl-xxx
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import time

sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")

import requests  # noqa: E402
from cubesandbox import Config, Sandbox  # noqa: E402
from cubesandbox._pty import PtySize  # noqa: E402

ENVD_PORT = 49983
MB = 1024 * 1024


def encode(data: bytes, flags: int = 0) -> bytes:
    return bytes([flags]) + struct.pack(">I", len(data)) + data


def stream(sandbox: Sandbox, cmd: str, mode: str, throttle: float = 0.0, timeout=300):
    url = f"http://{sandbox.get_host(ENVD_PORT)}/process.Process/Start"
    headers = {
        "Content-Type": "application/connect+json",
        "Connect-Protocol-Version": "1",
        "Connect-Content-Encoding": "identity",
    }
    token = sandbox._data.get("envdAccessToken")
    if token:
        headers["X-Access-Token"] = token
    body = encode(json.dumps({
        "process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}},
        "stdin": False,
    }).encode())

    t0 = time.monotonic()
    resp = requests.post(url, data=body, headers=headers, stream=True, timeout=timeout)
    stdout = 0
    raw = 0
    buffer = bytearray()
    errors: list[str] = []
    saw_end = False
    for chunk in resp.raw.stream(amt=8192, decode_content=False):
        raw += len(chunk)
        if throttle:
            time.sleep(throttle)
        if mode == "fast":
            continue
        buffer.extend(chunk)
        while len(buffer) >= 5:
            flags = buffer[0]
            size = struct.unpack(">I", buffer[1:5])[0]
            if len(buffer) < 5 + size:
                break
            payload = bytes(buffer[5:5 + size])
            del buffer[:5 + size]
            if flags & 0x02:
                errors.append(payload.decode("utf-8", "replace")[:200])
                continue
            event = json.loads(payload.decode()).get("event") or {}
            data = event.get("data") or {}
            if data.get("stdout"):
                stdout += len(base64.b64decode(data["stdout"]))
            if event.get("end") is not None:
                saw_end = True
    return {
        "stdout_bytes": stdout,
        "wire_bytes": raw,
        "saw_end": saw_end,
        "errors": errors,
        "seconds": round(time.monotonic() - t0, 2),
    }


def pty_flood(sandbox: Sandbox):
    handle = sandbox.pty.create(PtySize(rows=40, cols=120), timeout=60)
    received = 0
    errors: list[str] = []

    def pump(limit_s: float):
        nonlocal received
        end = time.monotonic() + limit_s
        try:
            for chunk in handle:
                received += len(chunk)
                if time.monotonic() > end:
                    return
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")

    # let the prompt arrive
    time.sleep(0.7)
    handle.send_stdin(b"yes abcdefghijklmnopqrstuvwxyz\n")
    pump(1.5)
    handle.send_stdin(b"\x03")
    time.sleep(0.3)
    handle.send_stdin(b"echo PTY-ALIVE-MARKER\n")
    pump(1.0)
    alive = False
    try:
        alive = handle.error is None
    except Exception:  # noqa: BLE001
        pass
    try:
        handle.kill()
    except Exception:  # noqa: BLE001
        pass
    return {"bytes_received": received, "errors": errors, "handle_error": alive}


def probe(sandbox: Sandbox, label: str) -> dict:
    out: dict = {"label": label, "sandbox_id": sandbox.sandbox_id}
    big = (b"log line " + b"x" * 71 + b"\n") * (64 * MB // 80)
    sandbox.files.write("/home/user/big.log", big)
    out["big_log_bytes"] = len(big)

    out["yes_fast_drain"] = stream(sandbox, "timeout 2 yes abcdefghijklmnopqrstuvwxyz0123456789", "fast")
    out["yes_sdk_like"] = stream(sandbox, "timeout 2 yes abcdefghijklmnopqrstuvwxyz0123456789", "sdk_like")
    out["yes_throttled_1ms"] = stream(sandbox, "timeout 2 yes abcdefghijklmnopqrstuvwxyz0123456789", "sdk_like", throttle=0.001)
    out["cat_50m_sdk_like"] = stream(sandbox, "cat /home/user/big.log", "sdk_like")
    out["pty_flood"] = pty_flood(sandbox)

    # after all that, is the sandbox's envd still healthy?
    out["health_after"] = sandbox.commands.run("echo STILL-ALIVE").stdout.strip()[:60]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--api-url", default="http://127.0.0.1:3000")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    config = Config(api_url=args.api_url, template_id=args.template)
    sandbox = Sandbox.create(timeout=900, config=config)
    try:
        result = probe(sandbox, args.label)
    finally:
        try:
            sandbox.kill()
        except Exception:  # noqa: BLE001
            pass
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
