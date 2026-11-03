# main.py -- resident PCM player: TCP 9200 -> I2S (16k/16bit/mono)
#
# Board: YD-ESP32-S3 (N16R8) + MAX98357A
#   BCLK=GPIO4, WS/LRC=GPIO5, DIN=GPIO6, GAIN float, SD float, GND common, Vin 3V3/5V
# Protocol: raw 16 kHz / 16-bit / mono PCM, streamed into TCP 9200.
#           one connection == one utterance; pre-buffer 1 s before opening the amp.
import machine
import socket
import time
import _thread

RATE = 16000
BCLK, WS, DIN = 4, 5, 6
PORT = 9200
PREBUF = 32000

i2s = machine.I2S(0, sck=machine.Pin(BCLK), ws=machine.Pin(WS), sd=machine.Pin(DIN),
                  mode=machine.I2S.TX, bits=16, format=machine.I2S.MONO,
                  rate=RATE, ibuf=16384)


def play_conn(c):
    c.settimeout(3)
    pre = b""
    while len(pre) < PREBUF:
        try:
            d = c.recv(4096)
        except OSError:
            break
        if not d:
            break
        pre += d
    if pre:
        i2s.write(pre)
    total = len(pre)
    idle = 0
    while True:
        try:
            d = c.recv(2048)
        except OSError:
            # 客户端卡死（PC 端崩溃/进程被杀）时不能无限等：
            # 连续 3 次超时（约 9 s 没数据）就收工，否则整个服务会被一个
            # 半死不活的连接占住，之后新连接能连上但永远没人读 ——
            # 表现就是 PC 端 [tts 失败] TimeoutError。
            idle += 1
            if idle >= 3:
                print("idle timeout", total)
                break
            continue
        if not d:
            break
        idle = 0
        i2s.write(d)
        total += len(d)
    return total


def serve():
    while True:
        srv = socket.socket()
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", PORT))
            srv.listen(2)
            while True:
                c, _ = srv.accept()
                try:
                    print("play", play_conn(c))
                except Exception as e:
                    print("play err", e)
                try:
                    c.close()
                except Exception:
                    pass
        except Exception as e:
            print("srv err", e)
            time.sleep(2)
        finally:
            try:
                srv.close()
            except Exception:
                pass


_thread.start_new_thread(serve, ())
print("audio server on", PORT)
