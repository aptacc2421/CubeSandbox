#!/usr/bin/env python3
import json, struct, sys, threading, time
sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
import requests
from cubesandbox import Config, Sandbox
H={"Content-Type":"application/connect+json","Connect-Protocol-Version":"1","Connect-Content-Encoding":"identity"}
def env(p):
    d=json.dumps(p).encode(); return bytes([0])+struct.pack(">I",len(d))+d
def reader(resp, cb, slow=None):
    buf=bytearray()
    try:
        for ch in resp.raw.stream(amt=8192, decode_content=False):
            buf.extend(ch)
            if slow: time.sleep(slow)
            while len(buf)>=5:
                f=buf[0]; n=struct.unpack(">I",buf[1:5])[0]
                if len(buf)<5+n: break
                p=bytes(buf[5:5+n]); del buf[:5+n]
                if f&2: continue
                try: ev=json.loads(p.decode()).get("event") or {}
                except Exception: continue
                if ev.get("start"): cb("start",ev["start"])
                if (ev.get("data") or {}).get("stdout"): cb("stdout",len(ev["data"]["stdout"]))
    except Exception: pass
s=Sandbox.create(timeout=600, config=Config(api_url="http://127.0.0.1:3000", template_id=sys.argv[1]))
r={"label":sys.argv[2]}; host=s.get_host(49983)
try:
    pid={}; n=[0]
    def cb(k,p):
        if k=="start": pid["pid"]=p["pid"]
        elif k=="stdout": n[0]+=p
    a=requests.post(f"http://{host}/process.Process/Start", data=env({"process":{"cmd":"/bin/bash","args":["-l","-c","yes zzzzzzzzzzzzzzzzzzzz"],"envs":{}},"stdin":False}), headers=H, stream=True, timeout=120)
    threading.Thread(target=reader,args=(a,cb),kwargs={"slow":0.15},daemon=True).start()
    t0=time.monotonic()
    while "pid" not in pid and time.monotonic()-t0<5: time.sleep(0.05)
    r["pid"]=pid.get("pid")
    time.sleep(2.5); a.close()
    time.sleep(3.0)
    r["yes_procs"]=s.commands.run("for p in /proc/[0-9]*; do c=$(cat $p/comm 2>/dev/null); [ \"$c\" = yes ] && echo \"$p comm=$c state=$(awk '{print $3}' $p/stat 2>/dev/null) wchan=$(cat $p/wchan 2>/dev/null)\"; done; echo END").stdout.strip()[:600]
    r["a_bytes_frozen"]=n[0]
    c_n=[0]; ev=[]
    def cb2(k,p):
        if k=="stdout": c_n[0]+=p
        else: ev.append(k)
    try:
        c=requests.post(f"http://{host}/process.Process/Connect", data=env({"process":{"pid":pid["pid"]}}), headers=H, stream=True, timeout=15)
        threading.Thread(target=reader,args=(c,cb2),daemon=True).start()
        time.sleep(3.0); r["reattach_bytes_3s"]=c_n[0]; r["reattach_events"]=ev[:4]; c.close()
    except Exception as e:
        r["reattach_err"]=f"{type(e).__name__}: {str(e)[:120]}"
    s.commands.run("pkill -9 yes || true")
finally:
    try: s.kill()
    except Exception: pass
print(json.dumps(r, indent=2, ensure_ascii=False))
