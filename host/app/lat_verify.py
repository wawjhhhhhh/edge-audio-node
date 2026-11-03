"""串口直连验证「延迟独立开关」（不依赖 web_server）。

为什么不用 com_verify.py：那个脚本走 HTTP -> web_server -> UDP，验证链路里
多了一层可能出错的地方。这次要证明的是固件行为，所以直接开串口、直接敲
字符，板子回显什么就是什么。

用法：
    python lat_verify.py            # 默认 COM3
    python lat_verify.py COM4
"""
import sys
import time

import com_log

# 期望的回显片段 -> 说明
CASES = [
    # (命令, 期望回显, 说明)
    ("0", 'dsp=0', '模式 -> 旁路'),
    ("i", 'lat=30', '延迟档 -> 300 ms'),
    ("2", 'dsp=2', '模式 -> 降噪门'),
    ("K", 'lat_off=1', '关闭延迟（门模式，100 fps 时生效）'),
    ("k", 'lat_off=0', '恢复延迟（门模式）'),
    ("K", 'lat_off=1', '再次关闭延迟'),
    ("i", 'lat=30', '关延迟状态下改档位：档位要记住'),
    ("k", 'lat_off=0', '恢复延迟：应回到 300 ms'),
    ("1", 'dsp=1', '模式 -> RNNoise'),
    ("K", 'lat_off=1', '关闭延迟（RNNoise：背压闸门被旁路）'),
    ("k", 'lat_off=0', '恢复延迟（RNNoise：回到 300 ms 背压）'),
    ("0", 'dsp=0', '回到旁路，收尾'),
]


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    h = com_log.open_port(port)
    time.sleep(0.3)
    com_log.read_for(h, 0.4)          # 冲掉上电旧日志

    print("串口 %s 已连接，逐条发送单字符命令\n" % port)
    passed = 0
    for cmd, want, note in CASES:
        com_log.write_bytes(h, cmd.encode())
        time.sleep(0.45)
        log = com_log.read_for(h, 0.5)
        lines = [l.strip() for l in log.replace("\r", "\n").split("\n") if l.strip()]
        hit = [l for l in lines if want in l]
        mark = "OK " if hit else "MISS"
        if hit:
            passed += 1
        print("  %s '%s' -> %-46s | %s"
              % (mark, cmd, note, hit[0] if hit else (lines[-1] if lines else "(无回显)")))

    com_log.k32.CloseHandle(h)
    print("\n通过 %d / %d" % (passed, len(CASES)))
    return 0 if passed == len(CASES) else 2


if __name__ == "__main__":
    sys.exit(main())
