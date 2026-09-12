#!/usr/bin/env python3
"""Experience probe A: how output *streams* back to a user.

An AI app reads a process's stdout incrementally, so what matters is not bulk
throughput but when each chunk arrives: time to first byte, per-token latency
and jitter, and the worst stall during a flood.

Talks the Connect-JSON protocol directly (same request the SDK builds) so the
arrival of every network chunk can be timestamped.

Usage: probe_stream.py --label stock --template tpl-xxx --out stock-stream.json
"""
from __future__ import annotations

import argparse
import base64
import json
import statistics
import struct
import sys
import time

sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")

import requests  # noqa: E402
from cubesandbox import Config, Sandbox  # noqa: E402

ENVD_PORT = 49983
CONNECT_CONTENT_TYPE = "application/connect+json"


def encode(data: bytes, flags: int = 0) -> bytes:
    return bytes([flags]) + struct.pack(">I", len(data)) + data


def start_stream(sandbox: Sandbox, cmd: str, timeout_s: float | None = None):
    """POST process.Process/Start and yield (t, kind, payload) as chunks arrive."""
    url = f"http://{sandbox.get_host(ENVD_PORT)}/process.Process/Start"
    headers = {
        "Content-Type": CONNECT_CONTENT_TYPE,
        "Connect-Protocol-Version": "1",
        "Connect-Content-Encoding": "identity",
    }
    token = sandbox._data.get("envdAccessToken")
    if token:
        headers["X-Access-Token"] = token
    if timeout_s:
        headers["Connect-Timeout-Ms"] = str(int(timeout_s * 1000))
    body = encode(json.dumps({
        "process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}},
        "stdin": False,
    }).encode())

    resp = requests.post(url, data=body, headers=headers, stream=True, timeout=300)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    buffer = bytearray()
    for chunk in resp.raw.stream(amt=8192, decode_content=False):
        now = time.monotonic()
        buffer.extend(chunk)
        while len(buffer) >= 5:
            flags = buffer[0]
            size = struct.unpack(">I", buffer[1:5])[0]
            if len(buffer) < 5 + size:
                break
            raw = bytes(buffer[5:5 + size])
            del buffer[:5 + size]
            if flags & 0x02:
                yield now, "end_stream", raw
                continue
            event = json.loads(raw.decode()).get("event") or {}
            data = event.get("data") or {}
            if data.get("stdout"):
                yield now, "stdout", base64.b64decode(data["stdout"])
            elif data.get("stderr"):
                yield now, "stderr", base64.b64decode(data["stderr"])
            elif event.get("end") is not None:
                yield now, "end", event["end"]


def timed_stream(sandbox: Sandbox, cmd: str, timeout_s: float | None = None):
    t0 = time.monotonic()
    first = None
    chunks: list[tuple[float, int]] = []
    total = 0
    end = None
    for now, kind, payload in start_stream(sandbox, cmd, timeout_s):
        if kind == "stdout":
            if first is None:
                first = now
            chunks.append((now, len(payload)))
            total += len(payload)
        elif kind == "end":
            end = payload
    done = time.monotonic()
    gaps = [round((chunks[i][0] - chunks[i - 1][0]) * 1000, 2) for i in range(1, len(chunks))]
    return {
        "ttfb_ms": round((first - t0) * 1000, 2) if first else None,
        "total_ms": round((done - t0) * 1000, 2),
        "bytes": total,
        "chunks": len(chunks),
        "max_gap_ms": max(gaps) if gaps else None,
        "gaps_ms": gaps[:60],
        "exit_code": (end or {}).get("exitCode"),
    }


def token_sim(sandbox: Sandbox, tokens: int = 60, interval: float = 0.05):
    """A process emitting one ~46-byte token every `interval` seconds."""
    cmd = ("python3 -u -c \"import time\n"
           f"for i in range({tokens}):\n"
           "    print('tok-%040d' % i, flush=True)\n"
           f"    time.sleep({interval})\"")
    t0 = time.monotonic()
    arrivals = []
    for now, kind, payload in start_stream(sandbox, cmd):
        if kind == "stdout":
            arrivals.append((now - t0, len(payload)))
    if not arrivals:
        return {"error": "no output"}
    # Each line is 'tok-...' + newline = 46 bytes; attribute arrivals to tokens.
    lags = []
    for idx, (arrival, _size) in enumerate(arrivals):
        expected = idx * interval
        lags.append(round((arrival - expected) * 1000, 2))
    deltas = [round((arrivals[i][0] - arrivals[i - 1][0]) * 1000, 2) for i in range(1, len(arrivals))]
    return {
        "tokens_expected": tokens,
        "tokens_observed": len(arrivals),
        "first_token_ms": round(arrivals[0][0] * 1000, 2),
        "last_token_ms": round(arrivals[-1][0] * 1000, 2),
        "per_token_lag_ms_median": round(statistics.median(lags), 2),
        "per_token_lag_ms_p95": round(sorted(lags)[int(len(lags) * 0.95) - 1], 2) if len(lags) > 1 else None,
        "inter_token_ms_median": round(statistics.median(deltas), 2) if deltas else None,
        "inter_token_ms_max": max(deltas) if deltas else None,
        "inter_token_ms": deltas[:20],
    }


def probe(sandbox: Sandbox, label: str) -> dict:
    out: dict = {"label": label, "sandbox_id": sandbox.sandbox_id}

    # 1. time to first byte of a trivial command
    out["echo"] = timed_stream(sandbox, "echo hello")

    # 2. per-token latency / jitter (AI streaming shape)
    out["token_sim_20ms"] = token_sim(sandbox, tokens=60, interval=0.02)
    out["token_sim_50ms"] = token_sim(sandbox, tokens=40, interval=0.05)

    # 3. bulk output: how fast a big burst drains, and the worst stall
    out["bulk_seq"] = timed_stream(sandbox, "seq 1 300000")

    # 4. flood for 2 s: steady-state rate and worst gap under backpressure
    flood = timed_stream(sandbox, "timeout 2 yes abcdefghijklmnopqrstuvwxyz0123456789")
    flood["mib_per_s"] = round(flood["bytes"] / (flood["total_ms"] / 1000) / 1048576, 1) if flood["total_ms"] else None
    out["flood_2s"] = flood

    # 5. TUI-style frames: 30 frames of 2 KiB every 100 ms
    cmd = ("python3 -u -c \"import time\n"
           "f = 'x' * 2048\n"
           "for i in range(30):\n"
           "    print('FRAME%02d' % i + f, flush=True)\n"
           "    time.sleep(0.1)\"")
    t0 = time.monotonic()
    frames = []
    for now, kind, payload in start_stream(sandbox, cmd):
        if kind == "stdout":
            frames.append(round((now - t0) * 1000, 2))
    out["tui_frames"] = {
        "frames": len(frames),
        "first_frame_ms": frames[0] if frames else None,
        "total_ms": frames[-1] if frames else None,
        "frame_gaps_ms": [round(frames[i] - frames[i - 1], 2) for i in range(1, len(frames))][:20],
    }

    # 6. cold start of a command that must fork+exec something real
    starts = []
    for _ in range(10):
        r = timed_stream(sandbox, "/bin/true")
        starts.append(r["ttfb_ms"] or r["total_ms"])
    out["true_10x_ms"] = {
        "median": round(statistics.median(starts), 2),
        "min": min(starts), "max": max(starts), "all": starts,
    }
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
