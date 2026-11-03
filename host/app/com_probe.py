"""诊断：往串口发一个已知字符，到底有没有回显？

用法：
    python com_probe.py COM3 i
"""
import sys
import time

import com_log

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
cmd = sys.argv[2] if len(sys.argv) > 2 else "i"

h = com_log.open_port(port)
time.sleep(0.3)
print("[清空前]", repr(com_log.read_for(h, 0.6)[-120:]))

print("[发送]", repr(cmd))
com_log.write_bytes(h, cmd.encode())
time.sleep(1.0)
print("[回显]", repr(com_log.read_for(h, 1.0)[-400:]))

com_log.k32.CloseHandle(h)
