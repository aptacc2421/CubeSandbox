#!/usr/bin/env python3
"""Where does the CPU go during one download?

Reads envd's own `/proc/<pid>/stat` (user/sys) and every thread's stat around
each download, once writing the result to a file and once discarding it, so the
server's cost can be separated from the client's ability to keep up.

Usage: cpu_split.py <template-id> [size-mb]
"""
import json
import struct
import subprocess
import sys
import time

sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
from cubesandbox import Config, Sandbox  # noqa: E402

REPS = 3


def body_for(cmd):
    p = json.dumps(
        {"process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}}, "stdin": False}
    ).encode()
    return bytes([0]) + struct.pack(">I", len(p)) + p


def proc_stat(s, pid):
    out = s.commands.run("awk '{print $14, $15, $16, $17}' /proc/%s/stat" % pid).stdout.split()
    utime, stime, cutime, cstime = (int(x) for x in out[:4])
    return {"u": utime, "s": stime, "cu": cutime, "cs": cstime}


def thread_stats(s, pid):
    out = s.commands.run(
        "for t in /proc/%s/task/*; do echo $(basename $t) $(cat $t/comm) "
        "$(awk '{print $14, $15}' $t/stat); done" % pid
    ).stdout
    threads = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4:
            threads[f[0]] = {"comm": f[1], "u": int(f[2]), "s": int(f[3])}
    return threads


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
        s.files.write(f"/home/user/ps{mb}.bin", b"x" * size)
        open("/tmp/req.bin", "wb").write(body_for(f"cat /home/user/ps{mb}.bin"))

        for i in range(REPS * 2):
            dest = "/dev/null" if i >= REPS else "/tmp/out.bin"
            before, tb = proc_stat(s, pid), thread_stats(s, pid)
            t0 = time.monotonic()
            subprocess.run(
                ["curl", "-sS", "-X", "POST", "--data-binary", "@/tmp/req.bin",
                 "-H", "Content-Type: application/connect+json",
                 "-H", "Connect-Protocol-Version: 1", "-H", f"X-Access-Token: {token}",
                 "-o", dest, "-w", "%{size_download}",
                 f"http://{host}/process.Process/Start"],
                capture_output=True, text=True, timeout=300)
            secs = time.monotonic() - t0
            after, ta = proc_stat(s, pid), thread_stats(s, pid)
            hot = []
            for tid, st in ta.items():
                prev = tb.get(tid, {"u": st["u"], "s": st["s"], "comm": st["comm"]})
                du, ds = st["u"] - prev["u"], st["s"] - prev["s"]
                if du + ds:
                    hot.append({"tid": tid, "comm": st["comm"],
                                "user_ms": round(du * 10), "sys_ms": round(ds * 10)})
            hot.sort(key=lambda h: -(h["user_ms"] + h["sys_ms"]))
            out["reps"].append({
                "dest": dest, "seconds": round(secs, 3),
                "MBps": round(size / secs / 1e6, 1),
                "envd_user_ms": (after["u"] - before["u"]) * 10,
                "envd_sys_ms": (after["s"] - before["s"]) * 10,
                "children_user_ms": (after["cu"] - before["cu"]) * 10,
                "children_sys_ms": (after["cs"] - before["cs"]) * 10,
                "hot_threads": hot[:5],
            })
    finally:
        try:
            s.kill()
        except Exception:
            pass
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
