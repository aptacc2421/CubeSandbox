import os, select, time
ep = select.epoll()
r, w = os.pipe()
ep.register(r, select.EPOLLIN)
N = 20000
t = time.perf_counter()
for _ in range(N):
    ep.poll(0)
poll0_us = (time.perf_counter() - t) / N * 1e6
os.write(w, b"x")
t = time.perf_counter()
for _ in range(N):
    os.write(w, b"y")
    ep.poll(0.001)
    os.read(r, 16)
ready_us = (time.perf_counter() - t) / N * 1e6
print("epoll_poll0_us=%.2f epoll_ready_roundtrip_us=%.2f" % (poll0_us, ready_us))
