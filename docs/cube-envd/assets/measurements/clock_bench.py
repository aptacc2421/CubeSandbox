import time
# CPU-bound busy loop: compare wall clock with CPU clocks.
t0w = time.perf_counter(); t0p = time.process_time(); t0t = time.thread_time()
x = 0
while time.perf_counter() - t0w < 1.0:
    x += 1
wall = time.perf_counter() - t0w
print("busy_loop_wall=%.3f process_time=%.3f thread_time=%.3f iters=%d" % (
    wall, time.process_time() - t0p, time.thread_time() - t0t, x))
# Two threads sleeping/waking each other: measure wakeup latency.
import threading, queue
q1, q2 = queue.Queue(), queue.Queue()
def worker():
    while True:
        v = q1.get()
        if v is None:
            return
        q2.put(v)
th = threading.Thread(target=worker, daemon=True); th.start()
n = 2000
t = time.perf_counter()
for i in range(n):
    q1.put(i); q2.get()
dt = time.perf_counter() - t
print("handoff_us_per_roundtrip=%.2f" % (dt / n * 1e6))
q1.put(None)
