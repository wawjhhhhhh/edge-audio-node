"""从 PC 控制台（`work/pc_app`，默认 8770）取板子的麦克风音频。

为什么要有这个模块
------------------
小智原来自己用 sounddevice 打开 PC 麦克风。可控制台里"开始收听"之后，板子的
ES8388 麦克风也在往 PC 送（UDP 50505 → JitterBuffer），于是同一个场景里出现了
两个麦克风源：小智听 PC 阵列，板子的声音只进了控制台。两边同时抢设备/同时开录，
听感上就是"冲突"。

现在改成**控制台是唯一的采集点**：
  板子麦克风 ──UDP 50505──> 控制台（JitterBuffer/增强/声码器）──HTTP /api/pcm──> 小智
好处有三个：
  1. 只有一个人打开设备，不会抢；
  2. 小智听到的就是控制台正在处理的那一路（开了降噪，它就听降噪后的）；
  3. 小智要"调板子"时，听到的和它自己刚下发的状态是同一个世界。

控制台没在收听时这里会明确报错，调用方可以选择回退到直接开 PC 麦克风
（见 `asr.py` 的 `source="auto"`）。

数据格式
--------
`/api/pcm?from=N` 返回**交织立体声 int16、48 kHz**，响应头：
    X-Pcm-Start / X-Pcm-Count / X-Pcm-Total   （单位都是"帧"，1 帧 = 2 个 int16）
这里做 48 kHz → 16 kHz 的 3:1 抽取：每 3 个样点取均值（粗低通），再取左右平均。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

import numpy as np

DEFAULT_BASE = "http://127.0.0.1:8770"
IN_RATE = 48000
OUT_RATE = 16000
DECIM = IN_RATE // OUT_RATE          # 3


class ConsoleMicError(RuntimeError):
    pass


class ConsoleMic:
    """把控制台的 /api/pcm 变成 16 kHz 单声道 PCM 块流。"""

    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 2.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = float(timeout)
        self.cursor = 0            # 已消费到第几"帧"（48 kHz 立体声帧）
        self._carry = np.zeros(0, dtype=np.int16)   # 16 kHz 单声道待发样点
        self._rem = None           # 3:1 抽取剩下的不足 3 的尾巴
        self.gaps = 0              # 跟不上 / 落后被丢掉的次数
        self.pulls = 0
        self.samples_out = 0
        self.stalls = 0

    # ---- 状态 -----------------------------------------------------------
    def state(self) -> dict | None:
        try:
            with urllib.request.urlopen(self.base + "/api/state", timeout=self.timeout) as r:
                import json
                return json.load(r)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def available(self) -> tuple[bool, str]:
        st = self.state()
        if st is None:
            return False, f"控制台没在跑（{self.base} 连不上）"
        if not st.get("listening"):
            return False, "控制台在跑，但没有收听（去点一下「开始收听」）"
        if not st.get("pcm_total"):
            return False, "控制台在收听，但板子一个音频包都没来"
        return True, "控制台正在收听，板子有音频"

    # ---- 拉取 -----------------------------------------------------------
    def _pull(self) -> bool:
        """拉一次 /api/pcm，把新样点接到 carry 后面。返回"拿到东西了没有"。"""
        url = "%s/api/pcm?from=%d" % (self.base, self.cursor)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                raw = r.read()
                start = int(r.headers.get("X-Pcm-Start") or 0)
                count = int(r.headers.get("X-Pcm-Count") or 0)
        except (urllib.error.URLError, OSError) as exc:
            raise ConsoleMicError("读取 /api/pcm 失败：%s" % exc) from exc
        self.pulls += 1
        if count <= 0 or not raw:
            return False

        if start > self.cursor:
            # 我们的游标落后于环形缓冲区最老的一块：中间那段永久丢了
            self.gaps += 1
            self.cursor = start
        elif start + count < self.cursor:
            # 缓冲区被清过（重新开始收听），从头接
            self.gaps += 1
            self.cursor = start

        frames = np.frombuffer(raw, dtype="<i2").reshape(-1, 2)
        if frames.shape[0] > count:
            frames = frames[:count]
        # 48 kHz → 16 kHz：先左右平均，再每 3 点取均值
        mono = frames.astype(np.float32).mean(axis=1)
        if self._rem is not None and self._rem.size:
            mono = np.concatenate([self._rem, mono])
        keep = (mono.size // DECIM) * DECIM
        if keep:
            mono = mono[:keep].reshape(-1, DECIM).mean(axis=1)
            self._carry = np.concatenate([self._carry, mono.astype(np.int16)])
        self._rem = mono[keep:] if keep else mono
        self.cursor = start + count
        return True

    # ---- 对外 -----------------------------------------------------------
    def blocks(self, block: int = 4000):
        """生成 16 kHz 单声道 PCM 字节块（默认 4000 样点 = 250 ms，和 asr.BLOCK 一致）。"""
        idle_since = time.time()
        while True:
            if self._carry.size >= block:
                chunk = self._carry[:block]
                self._carry = self._carry[block:]
                self.samples_out += int(chunk.size)
                idle_since = time.time()
                yield chunk.tobytes()
                continue
            got = self._pull()
            if got and self._carry.size:
                idle_since = time.time()
                continue
            # 没有新数据：控制台可能在收听但板子断流，也可能刚被暂停
            if time.time() - idle_since > 3.0:
                self.stalls += 1
                raise ConsoleMicError("3 秒没有新音频（板子断流，或控制台停止收听）")
            time.sleep(0.02)

    def reset(self) -> None:
        self.cursor = 0
        self._carry = np.zeros(0, dtype=np.int16)
        self._rem = None

    def stats(self) -> dict:
        return {"pulls": self.pulls, "gaps": self.gaps,
                "samples_out": self.samples_out, "stalls": self.stalls,
                "cursor": self.cursor}


if __name__ == "__main__":  # 手测：python -m work.xiaozhi.console_mic [秒数]
    import sys
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    mic = ConsoleMic()
    ok, why = mic.available()
    print("available:", ok, "-", why)
    if ok:
        t0 = time.time()
        total = 0
        for blk in mic.blocks():
            total += len(blk) // 2
            if time.time() - t0 >= secs:
                break
        print("got %d samples in %.2f s -> %.1f kSa/s" % (total, time.time() - t0,
                                                         total / max(1e-6, time.time() - t0) / 1000.0))
        print(mic.stats())
