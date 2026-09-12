import os, time
N = 20000
t = time.perf_counter()
for _ in range(N):
    os.getpid()
getpid_us = (time.perf_counter() - t) / N * 1e6
r, w = os.pipe()
buf = b"x" * 32768
pid = os.fork()
if pid == 0:
    os.close(r)
    try:
        for _ in range(2000):
            os.write(w, buf)
    finally:
        os._exit(0)
os.close(w)
t = time.perf_counter()
n = 0
while True:
    d = os.read(r, 32768)
    if not d:
        break
    n += len(d)
read_us = (time.perf_counter() - t) / max(1, n // 32768) * 1e6
os.waitpid(pid, 0)
print("getpid_us=%.2f pipe_read_32k_us=%.2f bytes=%d" % (getpid_us, read_us, n))
