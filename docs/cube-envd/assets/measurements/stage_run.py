import json, struct, subprocess, sys, time
sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
from cubesandbox import Config, Sandbox

def body_for(cmd):
    p = json.dumps({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}}, "stdin": False}).encode()
    return bytes([0]) + struct.pack(">I", len(p)) + p

tpl = sys.argv[1]
mb = int(sys.argv[2]) if len(sys.argv) > 2 else 32
size = mb * 1024 * 1024
s = Sandbox.create(timeout=900, config=Config(api_url="http://127.0.0.1:3000", template_id=tpl))
try:
    host = s.get_host(49983); token = s._data.get("envdAccessToken") or ""
    pid = s.commands.run("for p in /proc/[0-9]*; do readlink $p/exe 2>/dev/null | grep -q envd && basename $p && break; done").stdout.strip()
    print("envd pid", pid, s.commands.run("readlink /proc/%s/exe; cat /proc/%s/environ | tr '\\0' '\\n' | grep -i STAGE || echo NO-STAGE-ENV" % (pid, pid)).stdout.strip())
    s.files.write(f"/home/user/st{mb}.bin", b"x" * size)
    s.files.write("/tmp/req.bin", body_for(f"cat /home/user/st{mb}.bin"))
    g = ("curl -sS -X POST --data-binary @/tmp/req.bin -H 'Content-Type: application/connect+json' "
         "-H 'Connect-Protocol-Version: 1' -H 'X-Access-Token: %s' -o /dev/null "
         "-w '%%{size_download} %%{time_total}' http://127.0.0.1:49983/process.Process/Start" % token)
    print("guest-local:", s.commands.run(g + " 2>&1 | tail -1").stdout.strip())
    time.sleep(0.5)
    print("stage profile:\n" + (s.files.read("/tmp/envd-stage-profile.txt") or "<empty>"))
finally:
    s.kill()
