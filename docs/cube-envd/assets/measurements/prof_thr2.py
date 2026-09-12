#!/usr/bin/env python3
"""Throughput A/B *and* an in-guest profile of the envd process.

For each size: write the payload into the sandbox, start a sampler *inside* the
guest that records envd's per-thread kernel wait channel (`/proc/<tid>/wchan`),
its process-wide user/sys CPU and the TCP queues, then run the download several
times from the host and stop the sampler.

The sampler runs as a foreground command in a Python thread, because a
backgrounded child is killed as soon as its command session ends.

Printing the wait histogram next to the achieved MB/s says whether the time went
to userspace work, to the socket, or to waiting on something upstream.

Usage: prof_thr2.py <template-id> [sizes...]
"""
import json
import statistics
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
from cubesandbox import Config, Sandbox  # noqa: E402

SAMPLER = r"""
set -u
PID="$1"
: > /tmp/prof.log
while [ ! -f /tmp/prof.stop ]; do
  st=$(awk '{print $14" "$15}' "/proc/$PID/stat" 2>/dev/null)
  echo "SAMPLE ${st:-0 0}" >> /tmp/prof.log
  for t in /proc/$PID/task/*; do
    wc=$(cat "$t/wchan" 2>/dev/null)
    [ -n "$wc" ] && echo "WAIT $wc" >> /tmp/prof.log
  done
  awk '{split($5, a, ":"); if (a[1] != "") print "NET", $2, a[1]}' /proc/net/tcp 2>/dev/null >> /tmp/prof.log
  sleep 0.03
done
echo DONE >> /tmp/prof.log
"""

REPS = 5


def body_for(cmd):
    p = json.dumps(
        {"process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}}, "stdin": False}
    ).encode()
    return bytes([0]) + struct.pack(">I", len(p)) + p


def parse(log):
    """Split the log into samples and return the profile over the active window.

    A sample is active when envd's CPU grew since the previous one: the sampler
    also covers the idle time before and after the transfers.
    """
    samples, cur = [], None
    for line in log.splitlines():
        if line.startswith("SAMPLE "):
            if cur:
                samples.append(cur)
            try:
                u, s = (int(x) for x in line.split()[1:3])
            except ValueError:
                u = s = 0
            cur = {"cpu": u + s, "waits": {}, "tx_queue": 0, "states": {}}
        elif cur is not None and line.startswith("WAIT "):
            w = line.split()[1]
            cur["waits"][w] = cur["waits"].get(w, 0) + 1
        elif cur is not None and line.startswith("NET "):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    cur["tx_queue"] = max(cur["tx_queue"], int(parts[2], 16))
                except ValueError:
                    pass
    if cur:
        samples.append(cur)

    active = [
        s for i, s in enumerate(samples)
        if i and (s["cpu"] > samples[i - 1]["cpu"] or s["tx_queue"] > 0)
    ]
    window = active or samples
    waits, states = {}, {}
    tx_max = 0
    for s in window:
        for w, n in s["waits"].items():
            waits[w] = waits.get(w, 0) + n
        tx_max = max(tx_max, s["tx_queue"])
    if samples:
        states = {"active": len(active), "idle": len(samples) - len(active), "total": len(samples)}
    cpu = samples[-1]["cpu"] - samples[0]["cpu"] if len(samples) > 1 else 0
    return {
        "samples": states,
        "active_window_wait_channels": dict(sorted(waits.items(), key=lambda kv: -kv[1])[:8]),
        "cpu_s": round(cpu / 100.0, 2),
        "active_tx_queue_max": tx_max,
    }


def main():
    template = sys.argv[1]
    sizes = [int(a) for a in sys.argv[2:]] or [4, 32]
    config = Config(api_url="http://127.0.0.1:3000", template_id=template)
    s = Sandbox.create(timeout=900, config=config)
    out = {"template": template, "reps": REPS, "sizes": {}}
    try:
        host = s.get_host(49983)
        token = s._data.get("envdAccessToken") or ""
        s.files.write("/tmp/sampler.sh", SAMPLER.encode())
        pid = s.commands.run(
            "for p in /proc/[0-9]*; do readlink $p/exe 2>/dev/null | grep -q envd && basename $p && break; done"
        ).stdout.strip().splitlines()[-1]
        ident = s.commands.run(
            "readlink /proc/%s/exe; sha256sum $(readlink /proc/%s/exe) | cut -d' ' -f1" % (pid, pid)
        ).stdout.strip().splitlines()
        out["envd_pid"], out["envd_exe"] = pid, ident[0]
        out["envd_sha256"] = ident[-1]

        for mb in sizes:
            size = mb * 1024 * 1024
            s.files.write(f"/home/user/p{mb}.bin", b"x" * size)
            open("/tmp/req.bin", "wb").write(body_for(f"cat /home/user/p{mb}.bin"))
            s.commands.run("rm -f /tmp/prof.stop /tmp/prof.log")
            sampler = threading.Thread(
                target=lambda: s.commands.run("bash /tmp/sampler.sh %s" % pid), daemon=True
            )
            sampler.start()
            time.sleep(0.4)

            runs = []
            for _ in range(REPS):
                t0 = time.monotonic()
                subprocess.run(
                    ["curl", "-sS", "-X", "POST", "--data-binary", "@/tmp/req.bin",
                     "-H", "Content-Type: application/connect+json",
                     "-H", "Connect-Protocol-Version: 1", "-H", f"X-Access-Token: {token}",
                     "-o", "/tmp/out.bin", "-w", "%{size_download}",
                     f"http://{host}/process.Process/Start"],
                    capture_output=True, text=True, timeout=300)
                secs = time.monotonic() - t0
                blob = open("/tmp/out.bin", "rb").read()
                runs.append({"seconds": round(secs, 3), "MBps": round(size / secs / 1e6, 1),
                             "delivered": len(blob)})
            s.commands.run("touch /tmp/prof.stop")
            sampler.join(timeout=30)
            prof = parse(s.files.read("/tmp/prof.log"))
            mbps = [r["MBps"] for r in runs]
            out["sizes"][f"{mb}MiB"] = {
                "runs": runs,
                "MBps_min": min(mbps),
                "MBps_median": round(statistics.median(mbps), 1),
                "MBps_max": max(mbps),
                "delivered_ok": all(r["delivered"] >= size for r in runs),
                "prof": prof,
            }
    finally:
        try:
            s.kill()
        except Exception:
            pass
    print(json.dumps(out, indent=2, sort_keys=False))


if __name__ == "__main__":
    main()
