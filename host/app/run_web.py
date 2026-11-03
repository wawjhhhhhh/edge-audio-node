"""启动 Web 版控制台：本地服务 + 一个没有地址栏的独立窗口。

为什么用 `--app` 而不是 Electron：Edge/Chrome 自带 app 模式，渲染的就是一个
无地址栏、无标签栏的独立窗口，零额外运行时、零打包体积。窗口标题由页面 <title> 决定。
"""
import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_server

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_browser():
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=web_server.PORT)
    ap.add_argument("--no-window", action="store_true", help="只起服务，不开窗口")
    args = ap.parse_args()

    th = threading.Thread(target=web_server.serve,
                          kwargs={"port": args.port}, daemon=True)
    th.start()
    time.sleep(0.6)

    url = "http://127.0.0.1:%d/" % args.port
    if args.no_window:
        print("服务已起：%s  （Ctrl+C 退出）" % url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
        return

    exe = find_browser()
    if exe:
        profile = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".webprofile")
        os.makedirs(profile, exist_ok=True)
        cmd = [exe,
               "--app=" + url,
               "--window-size=1280,880",
               "--user-data-dir=" + profile,
               "--no-first-run",
               "--no-default-browser-check"]
        print("打开窗口：%s" % os.path.basename(exe))
        proc = subprocess.Popen(cmd)
        proc.wait()
    else:
        print("没找到 Edge/Chrome，用默认浏览器打开：%s" % url)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
