import json, struct, subprocess, sys, time
sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
from cubesandbox import Config, Sandbox

def body_for(cmd):
    p = json.dumps({"process": {"cmd": "/bin/bash", "args": ["-l", "-c", cmd], "envs": {}}, "stdin": False}).encode()
    return bytes([0]) + struct.pack(">I", len(p)) + p

PY = r'''
import time
N=2000; SZ=44*1024
t=time.time(); keep=[bytearray(SZ) for _ in range(N)]; fresh=time.time()-t
b=bytearray(SZ)
t=time.time()
for _ in range(N): b[:] = b
reuse=time.time()-t
del keep
t=time.time(); x=bytes(SZ*N); big=time.time()-t
print("fresh_alloc_ms_per_44k=%.3f reuse_ms_per=%.3f big_bytes_GBps=%.2f" % (fresh/N*1000, reuse/N*1000, SZ*N/big/1e9))
'''

tpl = sys.argv[1]
s = Sandbox.create(timeout=900, config=Config(api_url="http://127.0.0.1:3000", template_id=tpl))
try:
    host = s.get_host(49983); token = s._data.get("envdAccessToken") or ""
    pid = s.commands.run("for p in /proc/[0-9]*; do readlink $p/exe 2>/dev/null | grep -q envd && basename $p && break; done").stdout.strip()
    print("guest python:", s.commands.run("python3 -c '%s'" % PY.replace("\n", ";")).stdout.strip()[:200])
    size = 32*1024*1024
    s.files.write("/home/user/ap.bin", b"x"*size)
    bi = body_for("cat /home/user/ap.bin")
    s.files.write("/tmp/ap-req.bin", bi)
    open("/tmp/ap-req.bin","wb").write(bi)
    def faults():
        out = s.commands.run("awk '{print $10, $12}' /proc/%s/stat" % pid).stdout.split()
        return int(out[0]), int(out[1])
    f0 = faults()
    r = subprocess.run(["curl","-sS","-X","POST","--data-binary","@/tmp/ap-req.bin",
        "-H","Content-Type: application/connect+json","-H","Connect-Protocol-Version: 1",
        "-H","X-Access-Token: "+token,"-o","/dev/null","-w","%{size_download} %{time_total}",
        "http://%s/process.Process/Start" % host], capture_output=True, text=True, timeout=300)
    f1 = faults()
    print("host-client:", r.stdout.strip(), "minflt", f1[0]-f0[0], "majflt", f1[1]-f0[1])
finally:
    s.kill()
