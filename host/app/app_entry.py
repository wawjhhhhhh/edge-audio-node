"""统一启动器：起本地服务 + 开一个没有地址栏的独立窗口。

两个用途：
  1) 源码直接跑：`python app_entry.py`
  2) 打包成 EXE 的入口（PyInstaller 的 --add-data 把 web/ 带进去）

为什么不用 Electron：Electron 要打一整个 Chromium（100MB+），而我们已经有 Python 后端。
用 Edge/Chrome 的 `--app=URL` 就得到一个独立窗口，零额外运行时。

窗口关掉 -> 服务跟着退出，不会留下占端口的僵尸进程。
"""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

DEFAULT_PORT = 8770
BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def resource(rel):
    """打包后资源在 _MEIPASS 里；源码运行时就在脚本旁边。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def is_ours(port):
    """端口被占时，判断占用者是不是本程序（同一个 /api/state 协议）。"""
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=1.2) as r:
            return "board_ip" in json.loads(r.read().decode("utf-8"))
    except Exception:
        return False


def wait_up(port, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.4)
            s.close()
            return True
        except OSError:
            time.sleep(0.12)
    return False


def find_browser():
    for p in BROWSER_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def open_window(url):
    """返回浏览器进程；找不到浏览器则退回默认浏览器并返回 None。"""
    exe = find_browser()
    if not exe:
        webbrowser.open(url)
        return None
    prof = os.path.join(tempfile.gettempdir(), "zynq-audio-console-profile")
    try:
        os.makedirs(prof, exist_ok=True)
    except OSError:
        pass
    args = [
        exe,
        "--app=" + url,
        "--window-size=1400,920",
        "--user-data-dir=" + prof,
        "--no-first-run",
        "--no-default-browser-check",
        # --disable-gpu：这台机器上不加，Edge 的 GPU 进程会崩、窗口起不来
        "--disable-gpu",
        # --no-sandbox：受限会话（沙箱/某些企业环境）里没有它，渲染进程拉不起来 ->
        #   窗口是一片白。代价是 Edge 会在页面顶部挂一条"你使用的不是受支持的命令行
        #   标记"的黄色警告条，所以要配 --test-type 把它压掉。
        "--no-sandbox",
        "--test-type",
    ]
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        webbrowser.open(url)
        return None


def _logdir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def log(msg):
    """打包成 windowed 后没有 stdout，所有关键步骤都落盘，否则失败只能靠猜。"""
    line = "%s  %s\n" % (time.strftime("%H:%M:%S"), msg)
    for path in (os.path.join(_logdir(), "console-run.log"),
                 os.path.join(tempfile.gettempdir(), "zynq-console.log")):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except OSError:
            continue


def say(msg):
    """windowed 模式下没有控制台，stdout 可能是 None —— 直接 print 会抛异常。"""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def fatal(msg):
    """打包成 windowed 后没有控制台，出错必须弹窗，否则用户只看到闪一下。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(msg), "端侧音频控制台 · 启动失败", 0x10)
    except Exception:
        say(msg)


def main():
    server_only = "--server-only" in sys.argv
    port = DEFAULT_PORT
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    already = False
    if not port_free(port):
        if is_ours(port):
            already = True
        else:
            while port < DEFAULT_PORT + 20 and not port_free(port):
                port += 1

    url = "http://127.0.0.1:%d/" % port

    if not already:
        try:
            import web_server
            web_server.WEB_DIR = resource("web")
            web_server.EXIT_ON_IDLE = not server_only
            t = threading.Thread(target=web_server.serve, args=(port, "127.0.0.1"), daemon=True)
            t.start()
        except Exception as exc:
            fatal("服务启动失败：\n%r\n\n请确认 web/ 目录与本程序在同一位置。" % (exc,))
            return 1
        log("server started, waiting up")
        if not wait_up(port):
            fatal("服务没能监听 %d，可能被防火墙拦了。" % port)
            return 1
    else:
        say("检测到已在运行的服务，直接开窗口。")

    if server_only:
        say(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    t0 = time.time()
    proc = open_window(url)
    if proc is not None:
        while proc.poll() is None:
            time.sleep(0.3)
        alive = time.time() - t0
        log("浏览器退出，存活 %.1fs" % alive)
        if alive > 5.0:
            return 0                     # 正常：用户关了窗口
        # 异常：浏览器起来就退了（GPU 崩 / 被策略拦）。退回默认浏览器，
        # 服务继续跑，由心跳看门狗在页面不再上报时收尾。
        say("独立窗口没能打开，改用默认浏览器。")
        log("window failed, fallback to default browser")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    try:
        log("启动 frozen=%s argv=%s" % (getattr(sys, "frozen", False), sys.argv[1:]))
        log("_MEIPASS=%s" % getattr(sys, "_MEIPASS", "-"))
        rc = main()
        log("退出 rc=%s" % rc)
        sys.exit(rc or 0)
    except SystemExit:
        raise
    except BaseException:
        import traceback
        tb = traceback.format_exc()
        log("崩溃：\n" + tb)
        fatal(tb[-1200:])
        sys.exit(1)
