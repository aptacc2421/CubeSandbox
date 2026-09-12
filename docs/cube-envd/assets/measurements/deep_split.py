#!/usr/bin/env python3
"""Deep split of one download: where do the syscalls and packets go?

Per rep it records envd's own user/sys CPU, its read/write syscall counts
(`/proc/<pid>/io`), its context switches, and the guest NIC counters, and it
runs one rep with the client *inside* the guest so the host-side network path
can be excluded.

Usage: deep_split.py <template-id> [size-mb]
"""
import json
import struct
import subprocess
import sys
import time

sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
from cubesandbox import Config, Sandbox  # noqa: E402

CURL = ("curl -sS -X POST --data-binary @/tmp/req.bin "
        "-H 'Content-Type: application/connect+json' -H 'Connect-Protocol-Version: 1' "
        "-H 'X-Access-Token: TOKEN' -o /dev/null -w '%{size_download} %{time_total}' "
        "http://127.0.0.1:49983/process.Process/Start")


def body_for(cmd):
    p = json.dumps(
        {"process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}}, "stdin": False}
    ).encode()
    return bytes([0]) + struct.pack(">I", len(p)) + p


def counters(s, pid):
    raw = s.commands.run(
        "awk '{print $14, $15}' /proc/%s/stat; "
        "grep -E '^(syscr|syscw|rchar|wchar)' /proc/%s/io; "
        "grep -E '^(voluntary|nonvoluntary)' /proc/%s/status; "
        "cat /proc/net/dev" % (pid, pid, pid)
    ).stdout
    out = {}
    for line in raw.splitlines():
        f = line.replace(":", " ").split()
        if not f:
            continue
        if len(f) == 2 and f[0].isdigit():
            out["utime"], out["stime"] = int(f[0]), int(f[1])
        elif f[0] in ("syscr", "syscw", "rchar", "wchar"):
            out[f[0]] = int(f[1])
        elif f[0].startswith("voluntary"):
            out[f[0]] = int(f[1])
        elif f[0].startswith("nonvoluntary"):
            out[f[0]] = int(f[1])
        elif f[0] in ("eth0", "lo") and len(f) >= 10:
            out[f[0] + "_rx_bytes"] = int(f[1])
            out[f[0] + "_rx_pkts"] = int(f[2])
            out[f[0] + "_tx_bytes"] = int(f[9])
            out[f[0] + "_tx_pkts"] = int(f[10])
    return out


def delta(before, after, keys):
    return {k: after.get(k, 0) - before.get(k, 0) for k in keys if k in after or k in before}


KEYS = ["utime", "stime", "syscr", "syscw", "rchar", "wchar",
        "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches",
        "eth0_rx_bytes", "eth0_rx_pkts", "eth0_tx_bytes", "eth0_tx_pkts",
        "lo_rx_bytes", "lo_rx_pkts", "lo_tx_bytes", "lo_tx_pkts"]


def main():
    template = sys.argv[1]
    mb = int(sys.argv[2]) if len(sys.argv) > 2 else 32
    size = mb * 1024 * 1024
    config = Config(api_url="http://127.0.0.1:3000", template_id=template)
    s = Sandbox.create(timeout=900, config=config)
    out = {"template": template, "size_mb": mb, "reps": []}
    try:
        host = s.get_host(49983)
        token = s._data.get("envdAccessToken") or ""
        pid = s.commands.run(
            "for p in /proc/[0-9]*; do readlink $p/exe 2>/dev/null | grep -q envd && basename $p && break; done"
        ).stdout.strip().splitlines()[-1]
        out["envd_pid"] = pid
        out["envd_exe"] = s.commands.run("readlink /proc/%s/exe" % pid).stdout.strip()
        s.files.write(f"/home/user/ds{mb}.bin", b"x" * size)
        s.files.write("/tmp/req.bin", body_for(f"cat /home/user/ds{mb}.bin"))
        guest_curl = CURL.replace("TOKEN", token)

        host_curl = ["curl", "-sS", "-X", "POST", "--data-binary", "@/tmp/req.bin",
                     "-H", "Content-Type: application/connect+json",
                     "-H", "Connect-Protocol-Version: 1", "-H", f"X-Access-Token: {token}",
                     "-o", "/dev/null", "-w", "%{size_download} %{time_total}",
                     f"http://{host}/process.Process/Start"]

        for i in range(2):
            before = counters(s, pid)
            t0 = time.monotonic()
            r = subprocess.run(host_curl, capture_output=True, text=True, timeout=300)
            secs = time.monotonic() - t0
            after = counters(s, pid)
            d = delta(before, after, KEYS)
            out["reps"].append({
                "client": "host", "curl": r.stdout.strip(), "wall_s": round(secs, 3),
                "MBps": round(size / secs / 1e6, 1),
                "envd_user_ms": d["utime"] * 10, "envd_sys_ms": d["stime"] * 10,
                "d": d,
            })

        before = counters(s, pid)
        r = s.commands.run(guest_curl + " 2>&1 || echo GUEST-CURL-FAILED")
        after = counters(s, pid)
        d = delta(before, after, KEYS)
        reply = r.stdout.strip().splitlines()[-1]
        try:
            delivered, total = reply.split()
            mbps = round(int(delivered) / float(total) / 1e6, 1)
        except ValueError:
            delivered = total = mbps = None
        out["reps"].append({
            "client": "guest-localhost", "curl": reply, "MBps": mbps,
            "envd_user_ms": d["utime"] * 10, "envd_sys_ms": d["stime"] * 10, "d": d,
        })
    finally:
        try:
            s.kill()
        except Exception:
            pass
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
