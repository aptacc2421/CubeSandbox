import json, struct, sys
sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
from cubesandbox import Config, Sandbox

def body_for(cmd):
    p = json.dumps({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}}, "stdin": False}).encode()
    return bytes([0]) + struct.pack(">I", len(p)) + p

s = Sandbox.create(timeout=900, config=Config(api_url="http://127.0.0.1:3000", template_id=sys.argv[1]))
try:
    tok = s._data.get("envdAccessToken") or ""
    pid = s.commands.run("for p in /proc/[0-9]*; do readlink $p/exe 2>/dev/null | grep -q envd && basename $p && break; done").stdout.strip()
    s.commands.run("python3 -c \"open('/home/user/dd.bin','wb').write(b'x'*33554432)\"")
    s.files.write("/tmp/dd-req.bin", body_for("cat /home/user/dd.bin"))
    def cpu():
        o = s.commands.run("awk '{print $14, $15}' /proc/%s/stat" % pid).stdout.split()
        return int(o[0]) * 10, int(o[1]) * 10
    # (a) process stream
    u0, s0 = cpu()
    a = s.commands.run("curl -sS -X POST --data-binary @/tmp/dd-req.bin -H 'Content-Type: application/connect+json' "
                       "-H 'Connect-Protocol-Version: 1' -H 'X-Access-Token: %s' -o /dev/null "
                       "-w '%%{size_download} %%{time_total}' http://127.0.0.1:49983/process.Process/Start" % tok)
    u1, s1 = cpu()
    print("process-stream :", a.stdout.strip(), "envd_user+sys_ms=%d" % (u1 - u0 + s1 - s0))
    # (b) raw file download
    u0, s0 = cpu()
    b = s.commands.run("curl -sS -H 'X-Access-Token: %s' -o /dev/null -w '%%{size_download} %%{time_total}' "
                       "'http://127.0.0.1:49983/files?path=/home/user/dd.bin&username=user'" % tok)
    u1, s1 = cpu()
    print("files-download :", b.stdout.strip(), "envd_user+sys_ms=%d" % (u1 - u0 + s1 - s0))
finally:
    s.kill()
