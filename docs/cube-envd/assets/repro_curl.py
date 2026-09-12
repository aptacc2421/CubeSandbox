#!/usr/bin/env python3
"""Fast (C) client check: does a curl consumer survive the flood?"""
from __future__ import annotations
import argparse, base64, json, struct, subprocess, sys, time
sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
from cubesandbox import Config, Sandbox  # noqa: E402

MB = 1024 * 1024

def body_for(cmd: str) -> bytes:
    payload = json.dumps({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}}, "stdin": False}).encode()
    return bytes([0]) + struct.pack(">I", len(payload)) + payload

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True); ap.add_argument("--template", required=True)
    args = ap.parse_args()
    config = Config(api_url="http://127.0.0.1:3000", template_id=args.template)
    sandbox = Sandbox.create(timeout=900, config=config)
    out = {"label": args.label, "sandbox_id": sandbox.sandbox_id}
    try:
        big = (b"log line " + b"x" * 71 + b"\n") * (64 * MB // 80)
        sandbox.files.write("/home/user/big.log", big)
        host = sandbox.get_host(49983)
        token = sandbox._data.get("envdAccessToken") or ""
        for name, cmd in (("cat_50m_curl", "cat /home/user/big.log"),
                          ("yes_curl", "timeout 2 yes abcdefghijklmnopqrstuvwxyz0123456789")):
            open("/tmp/req.bin", "wb").write(body_for(cmd))
            t0 = time.monotonic()
            proc = subprocess.run([
                "curl", "-sS", "-X", "POST", "--data-binary", "@/tmp/req.bin",
                "-H", "Content-Type: application/connect+json",
                "-H", "Connect-Protocol-Version: 1",
                "-H", f"X-Access-Token: {token}",
                "-o", "/tmp/out.bin", "-w", "%{http_code} %{size_download}", f"http://{host}/process.Process/Start",
            ], capture_output=True, text=True, timeout=300)
            blob = open("/tmp/out.bin", "rb").read()
            out[name] = {
                "curl": proc.stdout.strip(),
                "bytes": len(blob),
                "seconds": round(time.monotonic() - t0, 2),
                "has_resource_exhausted": b"resource_exhausted" in blob,
                "has_end": b'"end"' in blob,
                "tail": blob[-160:].decode("utf-8", "replace"),
                "expected": len(big) if name.startswith("cat") else None,
            }
    finally:
        try: sandbox.kill()
        except Exception: pass
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0

raise SystemExit(main())
