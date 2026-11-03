"""把 index.html 里的内联 <script> 抽出来，交给 node --check 做语法校验。

用途：改完界面只做"看起来对"的检查是不够的 —— 一个多余的括号会让整页 JS 全死，
而表现是"页面在，按钮全没反应"，很容易被当成后端问题查半天。
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "web", "index.html")


def main():
    src = open(HTML, encoding="utf-8").read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S | re.I)
    print("内联 script 块数: %d" % len(blocks))
    bad = 0
    for i, body in enumerate(blocks):
        path = os.path.join(tempfile.gettempdir(), "ui_js_%d.js" % i)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        flag = "OK" if r.returncode == 0 else "SYNTAX ERROR"
        print("  块%d (%d 字节): %s" % (i, len(body), flag))
        if r.returncode != 0:
            bad += 1
            print(r.stderr.strip()[:1500])
    print("RESULT:", "ALL OK" if bad == 0 else "%d 块有语法错误" % bad)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())