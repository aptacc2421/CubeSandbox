#!/usr/bin/env python3
import json, struct, subprocess, sys, time
sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
from cubesandbox import Config, Sandbox

def body_for(cmd):
    p = json.dumps({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}}, "stdin": False}).encode()
    return bytes([0]) + struct.pack(">I", len(p)) + p

template = sys.argv[1]
config = Config(api_url="http://127.0.0.1:3000", template_id=template)
s = Sandbox.create(timeout=900, config=config)
res = {}
try:
    host = s.get_host(49983); token = s._data.get("envdAccessToken") or ""
    for mb in (1, 2, 3, 4, 6, 8, 16, 32):
        size = mb * 1024 * 1024
        s.files.write(f"/home/user/f{mb}.bin", b"x" * size)
        open("/tmp/req.bin", "wb").write(body_for(f"cat /home/user/f{mb}.bin"))
        t0 = time.monotonic()
        subprocess.run(["curl", "-sS", "-X", "POST", "--data-binary", "@/tmp/req.bin",
                        "-H", "Content-Type: application/connect+json",
                        "-H", "Connect-Protocol-Version: 1",
                        "-H", f"X-Access-Token: {token}",
                        "-o", "/tmp/out.bin", "-w", "%{size_download}", f"http://{host}/process.Process/Start"],
                       capture_output=True, text=True, timeout=300)
        blob = open("/tmp/out.bin", "rb").read()
        res[f"{mb}MiB"] = {"delivered": len(blob), "expected_min": size,
                           "exhausted": b"resource_exhausted" in blob, "end": b'"end"' in blob,
                           "seconds": round(time.monotonic() - t0, 2)}
finally:
    try: s.kill()
    except Exception: pass
print(json.dumps(res, indent=2, sort_keys=False))
