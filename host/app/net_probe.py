"""板级控制面探针：从 PC 发 UDP 控制命令，用 JTAG 读回固件里的真实状态。

为什么需要它：串口这条证据链目前不通（RX/TX 都收不到字节），
但"命令有没有被固件接受"这件事必须能验证。UDP 控制面（50507）
是 PC 控制台实际走的路径，而且命令按幂等设计，重复送达无害。

用法：
    # 发一个命令字符（单字节协议）
    python net_probe.py send b
    # 发 time-scale 两字节命令 'S' + 百分比
    python net_probe.py ts 75
    # 只监听 1 秒看有没有广播回流
    python net_probe.py sniff

注意：控制报文按历史实现走 UDP 广播，板子收到 2~3 份是正常的。
"""
import socket
import sys
import time

BOARD = "169.254.191.50"
PC = "169.254.191.246"
PORT_CTRL = 50507


def _sock():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return s


def send_raw(payload, repeat=3, gap=0.03):
    """重复发几份：UDP 有丢包可能，而所有控制命令都是幂等的。"""
    s = _sock()
    for i in range(repeat):
        s.sendto(payload, (BOARD, PORT_CTRL))
        # 同时往广播地址发一份：固件若绑在广播地址上也能收到
        try:
            s.sendto(payload, ("169.254.255.255", PORT_CTRL))
        except OSError:
            pass
        time.sleep(gap)
    s.close()
    return len(payload) * repeat * 2


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    op = sys.argv[1]

    if op == "send":
        ch = sys.argv[2]
        n = send_raw(ch.encode())
        print("已发 '%s'（%d 字节，含广播副本）-> %s:%d" % (ch, n, BOARD, PORT_CTRL))
        return 0

    if op == "ts":
        pct = int(sys.argv[2])
        if not (0 <= pct <= 255):
            print("百分比必须 0..255（越界用来测拒绝分支）")
            return 2
        n = send_raw(bytes([ord("S"), pct]))
        print("已发 'S'+%d（%d 字节，含广播副本）" % (pct, n))
        return 0

    if op == "sniff":
        secs = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", PORT_CTRL))
        s.settimeout(0.3)
        t0 = time.time()
        got = 0
        while time.time() - t0 < secs:
            try:
                d, a = s.recvfrom(2048)
            except socket.timeout:
                continue
            got += 1
            print("  %s:%d -> %d 字节 %s" % (a[0], a[1], len(d), d[:16].hex()))
        s.close()
        print("共 %d 包" % got)
        return 0

    print("未知操作 %s" % op)
    return 2


if __name__ == "__main__":
    sys.exit(main())
