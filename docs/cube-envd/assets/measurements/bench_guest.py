import time
N = 2000
SZ = 44 * 1024
t = time.time()
keep = [bytearray(SZ) for _ in range(N)]
fresh = time.time() - t
b = bytearray(SZ)
t = time.time()
for _ in range(N):
    b[:] = b
reuse = time.time() - t
del keep
t = time.time()
x = bytes(SZ * N)
big = time.time() - t
print("fresh_alloc_us_per_44k=%.1f reuse_us_per=%.1f big_copy_GBps=%.2f" % (
    fresh / N * 1e6, reuse / N * 1e6, SZ * N / big / 1e9))
