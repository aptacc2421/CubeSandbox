#!/usr/bin/env python3
"""Experience probe B: does the terminal feel laggy?

Measures what an agent/REPL session in a pty actually experiences:
keystroke echo latency, command round-trip, bulk output rate through the pty
and the worst stall, plus TUI-style frame cadence.

A reader thread owns the blocking stream iterator so the sender can time
individual keystrokes.

Usage: probe_pty_lat.py --label stock --template tpl-xxx --out stock-pty.json
"""
from __future__ import annotations

import argparse
import json
import queue
import statistics
import sys
import threading
import time

sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")

from cubesandbox import Config, Sandbox  # noqa: E402
from cubesandbox._pty import PtySize  # noqa: E402


class Pump:
    """Consume a PTY handle on a thread, recording (t, chunk) arrivals."""

    def __init__(self, handle):
        self.handle = handle
        self.q: queue.Queue = queue.Queue()
        self.chunks: list[tuple[float, int]] = []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            for chunk in self.handle:
                now = time.monotonic()
                self.chunks.append((now, len(chunk)))
                self.q.put(now)
                if self._stop.is_set():
                    break
        except Exception:  # noqa: BLE001
            pass

    def wait_output(self, timeout: float) -> bool:
        try:
            self.q.get(timeout=timeout)
            return True
        except queue.Empty:
            return False

    def drain(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def stop(self):
        self._stop.set()


def open_pty(sandbox: Sandbox, timeout: float = 60):
    handle = sandbox.pty.create(PtySize(rows=40, cols=120), timeout=timeout)
    pump = Pump(handle)
    pump.wait_output(5)          # first prompt
    time.sleep(0.3)
    pump.drain()
    return handle, pump


def probe(sandbox: Sandbox, label: str) -> dict:
    out: dict = {"label": label, "sandbox_id": sandbox.sandbox_id}
    replies = sandbox.commands

    # --- 1. keystroke echo latency --------------------------------------
    handle, pump = open_pty(sandbox)
    try:
        echoes = []
        for _ in range(20):
            pump.drain()
            t0 = time.monotonic()
            handle.send_stdin(b"x")
            if pump.wait_output(5):
                echoes.append(round((time.monotonic() - t0) * 1000, 2))
            time.sleep(0.05)
        handle.send_stdin(b"\x15")   # ctrl-U clears the line
        time.sleep(0.2)
        out["keystroke_echo_ms"] = {
            "n": len(echoes),
            "median": round(statistics.median(echoes), 2) if echoes else None,
            "min": min(echoes) if echoes else None,
            "max": max(echoes) if echoes else None,
            "all": echoes,
        }

        # --- 2. command round-trip ---------------------------------------
        rtts = []
        for i in range(10):
            pump.drain()
            marker = f"RTT{i:02d}"
            t0 = time.monotonic()
            handle.send_stdin(f"echo {marker}\n".encode())
            got = False
            deadline = time.monotonic() + 5
            buf = 0
            while time.monotonic() < deadline:
                if not pump.wait_output(1):
                    break
                buf += 1
                if buf >= 2:            # echo line + output line
                    got = True
                    break
            rtts.append(round((time.monotonic() - t0) * 1000, 2) if got else None)
        out["command_rtt_ms"] = {
            "n": len(rtts),
            "median": round(statistics.median([r for r in rtts if r]), 2) if any(rtts) else None,
            "all": rtts,
        }

        # --- 3. bulk output through the pty ------------------------------
        handle.send_stdin(b"python3 -c \"import sys\\nfor i in range(200000): sys.stdout.write('line-%06d abcdefghijklmnopqrstuvwxyz\\n' % i)\"\n")
        start_idx = len(pump.chunks)
        started = time.monotonic()
        pump.wait_output(120)
        # wait until it goes quiet for 1 s
        while True:
            if not pump.wait_output(1.5):
                break
            if time.monotonic() - started > 180:
                break
        elapsed = time.monotonic() - started
        chunk_slice = pump.chunks[start_idx:]
        total = sum(c[1] for c in chunk_slice)
        gaps = [round((chunk_slice[i][0] - chunk_slice[i - 1][0]) * 1000, 2) for i in range(1, len(chunk_slice))]
        out["pty_bulk"] = {
            "bytes": total,
            "chunks": len(chunk_slice),
            "seconds": round(elapsed, 2),
            "mib_per_s": round(total / elapsed / 1048576, 2) if elapsed else None,
            "max_gap_ms": max(gaps) if gaps else None,
            "gaps_over_200ms": [g for g in gaps if g > 200][:10],
        }

        # --- 4. TUI-style frame cadence in the pty ------------------------
        pump.drain()
        start_idx = len(pump.chunks)
        t0 = time.monotonic()
        handle.send_stdin(
            b"python3 -c \"import time\\nf='x'*2048\\n"
            b"for i in range(30):\\n print('FRAME%02d'%i+f, flush=True)\\n time.sleep(0.1)\"\n")
        frames = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(frames) < 30:
            if not pump.wait_output(1):
                break
            frames.append(round((pump.chunks[-1][0] - t0) * 1000, 2))
        prev = None
        cadence = []
        for f in frames:
            if prev is not None:
                cadence.append(round(f - prev, 2))
            prev = f
        out["pty_frames"] = {
            "frames": len(frames),
            "first_frame_ms": frames[0] if frames else None,
            "cadence_ms": cadence[:20],
            "cadence_median_ms": round(statistics.median(cadence), 2) if cadence else None,
        }
    finally:
        try:
            handle.kill()
        except Exception:  # noqa: BLE001
            pass
        pump.stop()

    # --- 5. plain (non-pty) command latency, for comparison --------------
    starts = []
    for _ in range(10):
        t0 = time.monotonic()
        replies.run("echo hi")
        starts.append(round((time.monotonic() - t0) * 1000, 2))
    out["command_run_ms"] = {
        "median": round(statistics.median(starts), 2),
        "min": min(starts), "max": max(starts), "all": starts,
    }

    # --- 6. pty session setup cost ---------------------------------------
    setups = []
    for _ in range(10):
        t0 = time.monotonic()
        h = sandbox.pty.create(PtySize(rows=40, cols=120), timeout=30)
        setups.append(round((time.monotonic() - t0) * 1000, 2))
        try:
            h.kill()
        except Exception:  # noqa: BLE001
            pass
    out["pty_create_ms"] = {
        "median": round(statistics.median(setups), 2),
        "min": min(setups), "max": max(setups), "all": setups,
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
