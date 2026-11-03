"""HTTP 层验收：起一个真服务（127.0.0.1:8781，不碰用户的 8770），用真 HTTP 读 /api/state。

单元测试证明的是 Hub.snapshot()；这里再证一遍 JSON 也是这样出去的：
  - 静默时 fps=0.0 / link=offline / under_rate≈100（旧版是 100.0，无法判活）
  - /  返回 200 且带上了新的"没有音频包到达"文案
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import udp_audio
import web_server

TEST_UDP = 50598
TEST_HTTP = 8781
LOG_PATH = os.path.join(tempfile.gettempdir(), "api_http_result.txt")

_orig = udp_audio.MicReceiver
web_server.MicReceiver = lambda **kw: _orig(port=TEST_UDP,
                                            **{k: v for k, v in kw.items() if k != "port"})

_out = io.StringIO()
_ok = True


def emit(line=""):
    _out.write(line + "\n")
    print(line)


def check(cond, what):
    global _ok
    emit(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        _ok = False


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (TEST_HTTP, path), timeout=5) as r:
        return r.status, r.read()


def main():
    threading.Thread(target=web_server.serve, kwargs={"port": TEST_HTTP, "host": "127.0.0.1"},
                     daemon=True).start()
    time.sleep(1.0)

    st, body = get("/")
    html = body.decode("utf-8", "replace")
    check(st == 200 and len(html) > 20000, "GET / 返回 200（%d 字节）" % len(body))
    # 新界面（macOS App 式）的契约：三入口 + 关键控件都在，且前端自己拉 /api/spectrum
    marks = ['id="seg"', 'data-p="recv"', 'data-p="play"', 'data-p="ai"',
             'id="dspSeg"', 'id="btnListen"', 'id="cvMini"', 'id="p-diag"', 'id="btnDiag"']
    missing = [m for m in marks if m not in html]
    check(not missing, "页面含三入口与关键控件%s" % ("" if not missing else "（缺 %s）" % missing))
    check("/api/state" in html and "/api/spectrum" in html,
          "前端会拉 /api/state 与 /api/spectrum")

    web_server.HUB.start_rx()
    emit("收听已开，UDP 端口 %d，HTTP %d" % (TEST_UDP, TEST_HTTP))
    snap = None
    for _ in range(25):
        st, body = get("/api/state")
        snap = json.loads(body)
        time.sleep(0.1)
    emit("  /api/state 关键字段: " + json.dumps(
        {k: snap.get(k) for k in ("listening", "fps", "link", "rx_age", "rx_fps",
                                  "rx_frames", "packets", "under", "under_rate")},
        ensure_ascii=False))
    check(st == 200, "GET /api/state 返回 200")
    check(snap.get("listening") is True, "listening=true")
    check(snap.get("fps") == 0.0, "静默时 fps=0.0（旧实现是 100.0 —— 本次修复的靶子）")
    check(snap.get("link") == "offline", "link=offline")
    check("rx_frames" in snap and snap["rx_frames"] == 0, "rx_frames=0")
    check(isinstance(snap.get("under_rate"), float) and snap["under_rate"] > 50,
          "under_rate 是浮点且 >50/s（%r）" % snap.get("under_rate"))

    web_server.HUB.stop_rx()
    emit()
    emit("RESULT: " + ("ALL PASS" if _ok else "SOME FAILED"))
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write(_out.getvalue())
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())