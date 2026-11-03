# boot.py -- always join the phone hotspot (8848), wherever we demo
#
# Why an unconditional disconnect: ESP-IDF caches the last SSID in NVS and may
# reconnect to it after reset. If we only connect when `isconnected()` is False,
# the board can silently stay on the old network.
import network
import time

WIFI_SSID = '8848'
WIFI_PASS = '20060207wen'

w = network.WLAN(network.STA_IF)
w.active(True)
try:
    w.config(pm=network.WLAN.PM_NONE)
except Exception:
    pass

try:
    if w.isconnected():
        w.disconnect()
        time.sleep(0.5)
except Exception:
    pass

try:
    w.connect(WIFI_SSID, WIFI_PASS)
except Exception as e:
    print('boot wifi err', e)

t0 = time.time()
while time.time() - t0 < 15:
    if w.isconnected():
        break
    time.sleep(0.5)

try:
    if w.isconnected():
        print('boot wifi OK', w.ifconfig()[0])
    else:
        print('boot wifi FAIL', w.status())
except Exception:
    pass
