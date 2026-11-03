# -*- coding: utf-8 -*-
"""Golden-bit check: listen on 50508 (status) + 50505 (audio uplink) without the console."""
import socket, time, collections, sys

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0

socks = {}
for port in (50508, 50505):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind(("0.0.0.0", port))
    except OSError as e:
        print("bind %d failed: %s" % (port, e))
        continue
    s.setblocking(False)
    socks[port] = s

cnt = collections.Counter()
srcs = collections.defaultdict(set)
last = {}
sizes = collections.defaultdict(collections.Counter)

t0 = time.time()
while time.time() - t0 < DUR:
    for port, s in socks.items():
        while True:
            try:
                d, a = s.recvfrom(4096)
            except BlockingIOError:
                break
            except OSError:
                break
            cnt[port] += 1
            srcs[port].add(a[0])
            sizes[port][len(d)] += 1
            last[port] = d

for port in sorted(socks):
    print("port %d: packets=%d (%.1f/s) srcs=%s sizes=%s" % (
        port, cnt[port], cnt[port] / DUR, sorted(srcs[port]), dict(sizes[port].most_common(3))))

if 50508 in last:
    d = last[50508]
    print("50508 raw:", d.hex())
    if len(d) >= 20 and d[0:2] == b"AS":
        p = d[8:20]
        ver, mode, ts, ap, dly, lat, ghi, glo, rn, tone, x = p[1:12]
        print("  ver=%d mode=%d ts_rate=%d ts_apply=%d dly=%d lat_off=%d gain=%d rn=%d tone=%d" % (
            ver, mode, ts, ap, dly, lat, (ghi << 8) | glo, rn, tone))
if 50505 in last:
    print("50505 first bytes:", last[50505][:16].hex(), "len", len(last[50505]))
