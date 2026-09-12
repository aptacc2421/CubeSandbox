import sys
sys.path.insert(0, "/root/cubesandbox-sdk/sdk/python")
from cubesandbox import Config, Sandbox
tpl = sys.argv[1]
s = Sandbox.create(timeout=600, config=Config(api_url="http://127.0.0.1:3000", template_id=tpl))
try:
    cmd = ("echo nproc=$(nproc); grep -c ^processor /proc/cpuinfo; "
           "cat /sys/fs/cgroup/cpu.max 2>/dev/null || cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null; "
           "free -m | head -2; cat /proc/version; "
           "for t in perf strace gdb python3 apt-get curl bash; do printf '%s=%s ' $t $(command -v $t || echo -); done; echo")
    r = s.commands.run(cmd)
    print(r.stdout)
    print("STDERR:", r.stderr[:400])
finally:
    s.kill()
