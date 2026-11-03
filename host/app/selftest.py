#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py -- 不需要板子的自测

覆盖：
  报文布局 / 一帧拆两包 / 真实 UDP 回环重组（样本逐位一致）/ seq 丢帧检测与 16 位回绕 /
  乱序与重复包 / WAV 往返 / 单声道 24 kHz → 立体声 48 kHz 转换 / 抖动缓冲欠载·溢出·漂移修剪 /
  dBFS 数学 / 协议容错（total=1920 的固件实际值）/ 440 Hz 频率 / MicReceiver 线程收流

用法：python selftest.py        （无需网线、无需板子、不打开声卡）
"""

from __future__ import annotations

import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import udp_audio as ua
from udp_audio import (
    CHANNELS, CHUNK_BYTES, FRAME_BYTES, FRAME_SAMPLES, HEADER_BYTES, MAGIC,
    SAMPLE_RATE, BoardControl, BoardCtl, BoardSender, FrameAssembler, JitterBuffer,
    FW_CTL_PORT, fw_ctl_frame,
    MicReceiver, ProtocolError, RingTap, SpectrumAnalyzer, WaveformView,
    WavRecorder, apply_gain, bytes_to_frame, mono_mix, pack_control, pack_frame,
    pack_packet, read_wav, rms_dbfs, tone, unpack_control, unpack_packet,
    write_wav,
)

RESULTS = []
TMP = None


def check(name: str, fn) -> None:
    t0 = time.perf_counter()
    try:
        detail = fn() or ""
        RESULTS.append((name, True, detail, time.perf_counter() - t0))
    except Exception as exc:
        RESULTS.append((name, False, "%s: %s" % (type(exc).__name__, exc),
                        time.perf_counter() - t0))
        if os.environ.get("SELFTEST_TRACE"):
            traceback.print_exc()


def need(cond, msg="") -> None:
    if not cond:
        raise AssertionError(msg or "assertion failed")


# ------------------------------------------------------------------ 用例

def t_header_layout():
    pkt = pack_packet(bytes(960), seq=0x1234, chunk=1, total=1920)
    need(len(pkt) == HEADER_BYTES + CHUNK_BYTES, "包长 %d" % len(pkt))
    need(pkt[:2] == MAGIC, "magic")
    b = struct.unpack(">HHH", pkt[2:8])
    need(b == (0x1234, 1, 1920), "头字段 %r" % (b,))
    p = unpack_packet(pkt)
    need((p.seq, p.chunk, p.total, p.samples) == (0x1234, 1, 1920, 240), "解析 %r" % (p,))
    return "8 B 头 + 960 B PCM，seq/chunk/total 大端"


def t_pack_frame():
    arr = tone(440.0, 0.01)                     # 恰好 480 样本
    need(arr.shape == (FRAME_SAMPLES, 2), "形状 %r" % (arr.shape,))
    p0, p1 = pack_frame(arr, seq=7)
    raw = arr.astype("<i2").tobytes()
    need(len(p0) == 968 and len(p1) == 968, "两包长度 %d/%d" % (len(p0), len(p1)))
    need(p0[8:] == raw[:CHUNK_BYTES], "chunk0 PCM")
    need(p1[8:] == raw[CHUNK_BYTES:], "chunk1 PCM")
    need(unpack_packet(p0).chunk == 0 and unpack_packet(p1).chunk == 1, "chunk 序号")
    return "一帧 1920 B → 2 包 × 968 B，PCM 切片一致"


def t_loopback_udp():
    """真实 UDP socket 回环：100 帧，重组后必须与原样本逐位相同。"""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2.0)
    port = rx.getsockname()[1]
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    src = tone(440.0, 1.0)                      # 100 帧
    n_frames = src.shape[0] // FRAME_SAMPLES
    pkts = []
    for i in range(n_frames):
        pkts.extend(pack_frame(src[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES], i))

    def _blast():                               # 边发边收，避免把 socket 缓冲打满
        for p in pkts:
            tx.sendto(p, ("127.0.0.1", port))

    th = threading.Thread(target=_blast, daemon=True)
    th.start()

    asm = FrameAssembler()
    got, n_pkt = [], 0
    deadline = time.monotonic() + 2.0
    while len(got) < n_frames and time.monotonic() < deadline:
        try:
            data, _ = rx.recvfrom(4096)
        except socket.timeout:
            break
        n_pkt += 1
        f = asm.push(unpack_packet(data))
        if f is not None:
            got.append(f)
    th.join(timeout=2.0)
    rx.close()
    tx.close()

    need(n_pkt == 2 * n_frames, "收到 %d 包" % n_pkt)
    need(len(got) == n_frames, "重组 %d/%d 帧" % (len(got), n_frames))
    merged = np.concatenate(got, axis=0)
    need(merged.shape == src.shape, "形状 %r" % (merged.shape,))
    diff = int(np.max(np.abs(merged.astype(np.int32) - src.astype(np.int32))))
    need(diff == 0, "样本最大偏差 %d" % diff)
    need(asm.lost == 0 and asm.dups == 0, "lost=%d dups=%d" % (asm.lost, asm.dups))
    return "%d 帧 / %d 包，最大样本偏差 0，丢帧 0" % (n_frames, n_pkt)


def t_seq_loss():
    asm = FrameAssembler()
    one = tone(440.0, 0.01)
    for seq in (0, 1, 3, 4, 5):                 # 故意漏掉 2
        for p in pack_frame(one, seq):
            asm.push(unpack_packet(p))
    need(asm.lost == 1, "lost=%d（期望 1）" % asm.lost)

    wrap = FrameAssembler()
    for seq in (65534, 65535, 0, 1):            # 16 位回绕不能算丢帧
        for p in pack_frame(one, seq):
            wrap.push(unpack_packet(p))
    need(wrap.lost == 0, "回绕时 lost=%d（期望 0）" % wrap.lost)
    need(wrap.frames == 4, "回绕 frames=%d" % wrap.frames)
    return "漏 1 帧被检出；65535→0 回绕不误报"


def t_reorder_dup():
    arr = tone(1000.0, 0.01)
    p0, p1 = pack_frame(arr, 10)
    asm = FrameAssembler()
    need(asm.push(unpack_packet(p1)) is None, "只有 chunk1 不该出帧")
    f = asm.push(unpack_packet(p0))
    need(f is not None, "chunk1 先到、chunk0 后到应能出帧")
    need(int(np.max(np.abs(f.astype(np.int32) - arr.astype(np.int32)))) == 0, "乱序重组样本")
    need(asm.dups == 0 and asm.frames == 1, "dups=%d frames=%d" % (asm.dups, asm.frames))
    need(asm.dups == 0 and asm.frames == 1, "dups=%d frames=%d" % (asm.dups, asm.frames))
    # 同一 chunk 重复到达（帧还没凑齐）
    dup = FrameAssembler()
    need(dup.push(unpack_packet(p1)) is None, "首个 chunk1")
    need(dup.push(unpack_packet(p1)) is None, "重复 chunk1 不该出帧")
    need(dup.dups == 1, "重复 chunk 未计数 dups=%d" % dup.dups)
    # 整帧重复：p0,p1 再来一遍，不能再出一帧
    asm.push(unpack_packet(p0))
    need(asm.push(unpack_packet(p1)) is None, "整帧重复不该出帧")
    need(asm.frames == 1, "整帧重复后 frames=%d" % asm.frames)
    return "乱序可重组；重复 chunk 与整帧重复都被丢弃（dups=%d/%d）" % (dup.dups, asm.dups)


def t_protocol_tolerance():
    arr = tone(440.0, 0.01)
    raw = arr.astype("<i2").tobytes()
    for total in (480, 1920, 0):                # 规范 480 / 固件实际 1920 / 乱填
        p = unpack_packet(pack_packet(raw[:CHUNK_BYTES], 1, 0, total))
        need(p.total == total, "total 透传")
    bad = [
        (b"XX" + bytes(6) + bytes(960), "错误 magic"),
        (b"AU" + bytes(4), "头不全"),
        (struct.pack(">2sHHH", MAGIC, 1, 5, 480) + bytes(960), "非法 chunk"),
    ]
    for data, why in bad:
        try:
            unpack_packet(data)
        except ProtocolError:
            pass
        else:
            raise AssertionError("应拒绝：%s" % why)
    need(unpack_packet(pack_packet(bytes(960), 1, 0, 480) + b"\x00\x00").samples == 240,
         "尾部填充应被容忍")
    return "接受 total=480/1920，拒绝坏 magic / 短包 / 非法 chunk"


def t_wav_roundtrip():
    arr = tone(440.0, 0.5, 0.25)                # 24000 样本 = 0.5 s
    path = os.path.join(TMP, "roundtrip.wav")
    write_wav(path, arr)
    need(os.path.getsize(path) == 44 + arr.size * 2, "文件大小 %d" % os.path.getsize(path))
    back, info = read_wav(path)
    need(back.shape == arr.shape, "形状 %r vs %r" % (back.shape, arr.shape))
    need(int(np.max(np.abs(back.astype(np.int32) - arr.astype(np.int32)))) == 0, "样本往返")
    need(info["rate"] == SAMPLE_RATE and not info["resampled"], "采样率 %r" % (info,))
    need(info["channels"] == 2 and not info["mono_to_stereo"], "声道 %r" % (info,))
    need(abs(info["duration"] - 0.5) < 1e-6, "时长 %.6f" % info["duration"])
    return "写→读 24000 样本逐位一致，%.3f s / %d Hz / 2 ch" % (info["duration"], info["rate"])


def t_wav_convert_mono_24k():
    """手工造 24 kHz 单声道 WAV，检查 转 48 kHz 立体声。"""
    n = 24000                                    # 1 s
    t = np.arange(n) / 24000.0
    v = np.round(np.sin(2 * np.pi * 440.0 * t) * 8000).astype("<i2")
    path = os.path.join(TMP, "mono24k.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(v.tobytes())

    arr, info = read_wav(path)
    need(info["resampled"] is True and info["mono_to_stereo"] is True, "转换标志 %r" % (info,))
    need(arr.shape[1] == 2, "声道数 %d" % arr.shape[1])
    need(abs(arr.shape[0] - 48000) <= FRAME_SAMPLES, "输出样本 %d（期望约 48000）" % arr.shape[0])
    need(np.array_equal(arr[:, 0], arr[:, 1]), "单声道复制后 L/R 应相同")
    need(abs(info["duration"] - 1.0) < 0.02, "时长 %.4f s" % info["duration"])
    peak = int(np.max(np.abs(arr.astype(np.int32))))
    need(abs(peak - 8000) < 400, "峰值 %d（期望≈8000）" % peak)
    return "24 kHz 单声道 → 48 kHz 立体声，%d 样本 / %.3f s / 峰值 %d" % (
        arr.shape[0], info["duration"], peak)


def t_jitter_underflow():
    jb = JitterBuffer()
    out = jb.pull(FRAME_SAMPLES)
    need(out.shape == (FRAME_SAMPLES, 2) and out.dtype == np.int16, "形状/类型")
    need(int(np.max(np.abs(out))) == 0, "空缓冲应输出静音")
    need(jb.underloads == 1, "underloads=%d" % jb.underloads)
    jb.push(np.full((FRAME_SAMPLES, 2), 1000, np.int16))
    out = jb.pull(FRAME_SAMPLES)
    need(int(np.max(out)) == 1000, "有数据时不应补静音")
    need(jb.underloads == 1, "有数据时不该增加 underload")
    return "空缓冲补静音并计 1 次 underload；有数据不误计"


def t_jitter_overflow():
    jb = JitterBuffer()
    blk = np.full((FRAME_SAMPLES, 2), 1234, np.int16)
    for _ in range(12):                          # 稳态 120 ms，只做温和修剪
        jb.push(blk)
    need(jb.overflows == 0, "稳态不该溢出（overflows=%d）" % jb.overflows)
    need(jb.depth_ms <= JitterBuffer.TRIM_HIGH_MS + 10.0,
         "稳态深度 %.1f ms" % jb.depth_ms)

    jb.push(np.full((FRAME_SAMPLES * 45, 2), 1234, np.int16))   # 一次突发 450 ms
    need(jb.overflows >= 1, "突发 450 ms 应触发溢出（overflows=%d）" % jb.overflows)
    need(jb.depth_ms <= JitterBuffer.TARGET_MAX_MS + 10.0,
         "溢出后深度 %.1f ms 未回到目标区间" % jb.depth_ms)
    out = jb.pull(FRAME_SAMPLES)
    need(int(np.max(out)) == 1234, "溢出后仍能出正常数据")
    return "稳态 %.0f ms 不溢出；突发 450 ms 触发 %d 次溢出并回落到 %.0f ms" % (
        JitterBuffer.TARGET_MAX_MS, jb.overflows, jb.depth_ms)


def t_jitter_drift():
    """板子 100.1 帧/秒、声卡 100 块/秒，跑 600 s 检查深度不无限增长。"""
    jb = JitterBuffer()
    jb.prefill_to(60.0)
    blk = np.zeros((FRAME_SAMPLES, 2), np.int16)
    acc = 0.0
    max_depth = 0.0
    steps = 60000
    for _ in range(steps):
        jb.pull(FRAME_SAMPLES)                   # 声卡每 10 ms 取一块
        acc += 1.001                             # 板子每 10 ms 推进 1.001 帧
        while acc >= 1.0:
            jb.push(blk)
            acc -= 1.0
        if jb.depth_ms > max_depth:
            max_depth = jb.depth_ms
    need(jb.trims > 0, "应发生漂移修剪（trims=%d）" % jb.trims)
    need(jb.overflows == 0, "不该走到 400 ms 溢出（overflows=%d）" % jb.overflows)
    need(max_depth <= JitterBuffer.TRIM_HIGH_MS + 10.0,
         "最大深度 %.1f ms 超出 %.0f" % (max_depth, JitterBuffer.TRIM_HIGH_MS))
    need(jb.depth_ms <= JitterBuffer.TRIM_HIGH_MS + 10.0,
         "末态深度 %.1f ms" % jb.depth_ms)
    return ("模拟 %.0f s、+0.1%% 时钟差：最大深度 %.0f ms，修剪 %d 次，溢出 0 次"
            % (steps / 100.0, max_depth, jb.trims))


def t_dbfs():
    full = np.full((FRAME_SAMPLES, 2), 32767, np.int16)   # 满量程方波
    rms, dbfs = rms_dbfs(full)
    need(abs(rms - 1.0) < 1e-3, "满量程 rms=%.4f" % rms)
    need(abs(dbfs) < 0.01, "满量程 dBFS=%.3f" % dbfs)
    sine = tone(1000.0, 0.1, 1.0)
    rms, dbfs = rms_dbfs(sine)
    need(abs(dbfs + 3.01) < 0.3, "正弦 dBFS=%.2f（期望 -3.01）" % dbfs)
    zero = np.zeros((FRAME_SAMPLES, 2), np.int16)
    need(rms_dbfs(zero) == (0.0, -120.0), "静音应为 -120 dBFS")
    return "满量程 0.0 dBFS，正弦 -3.0 dBFS，静音 -120 dBFS"


def t_tone_freq():
    x = tone(440.0, 1.0, 0.5)[:, 0].astype(np.int32)
    s = np.sign(x)
    s = s[s != 0]
    zc = int(np.sum(s[1:] != s[:-1]))
    need(abs(zc - 880) <= 2, "过零 %d 次（440 Hz 1 s 应约 880）" % zc)
    need(x.shape[0] == 48000, "样本数 %d" % x.shape[0])
    return "1 s 内过零 %d 次（≈880，即 440 Hz），48000 样本" % zc


def t_mic_receiver_thread():
    """真正跑一次接收线程（本机回环，不用板子）。"""
    rx = MicReceiver(port=0, bind_ip="127.0.0.1")
    rx.start()
    try:
        port = rx.sock.getsockname()[1]
        tx = BoardSender(dest="127.0.0.1", port=port)
        arr = tone(440.0, 0.2)                   # 20 帧
        n = arr.shape[0] // FRAME_SAMPLES
        for i in range(n):
            tx.send_frame(arr[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES], seq=i)
        tx.close()

        t0 = time.monotonic()
        while rx.frames < n and time.monotonic() - t0 < 3.0:
            time.sleep(0.01)
        snap = rx.snapshot()
        need(snap["frames"] == n, "收到 %d/%d 帧" % (snap["frames"], n))
        need(snap["packets"] == 2 * n, "包数 %d" % snap["packets"])
        need(snap["lost"] == 0, "lost=%d" % snap["lost"])
        need(snap["depth_ms"] > 0, "抖动缓冲深度 %.1f ms" % snap["depth_ms"])
        need(snap["dbfs"] > -30.0, "dBFS %.1f 太低" % snap["dbfs"])
        need(snap["sources"] == ["127.0.0.1"], "来源 %r" % (snap["sources"],))
        return "线程收到 %d 帧 / %d 包，深度 %.0f ms，%.1f dBFS" % (
            snap["frames"], snap["packets"], snap["depth_ms"], snap["dbfs"])
    finally:
        rx.close()


def t_sender_pacing():
    """发送节拍：1 s 音频应当在 ~1 s 内发完（允许 ±25%），且帧序号连续回绕。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.5)
    port = sock.getsockname()[1]
    sender = BoardSender(dest="127.0.0.1", port=port, prime_frames=2)
    arr = tone(440.0, 1.0)
    t0 = time.perf_counter()
    res = sender.play_array(arr)
    dt = time.perf_counter() - t0
    sender.close()
    got = 0
    seqs = []
    while True:
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            break
        p = unpack_packet(data)
        got += 1
        seqs.append(p.seq)
    sock.close()
    need(res["frames"] == 100, "发出 %d 帧" % res["frames"])
    need(got == 200, "对端收到 %d 包" % got)
    need(0.75 <= dt <= 1.35, "耗时 %.2f s" % dt)
    need(seqs == [s for s in range(100) for _ in (0, 1)], "seq 序列不连续")
    return "100 帧 / 200 包，耗时 %.2f s（实时），seq 0..99 各 2 包" % dt


# ---------------------------------------------------------------- V1.1 / V1.2 新用例

def t_adaptive_target():
    """16 自适应目标：稳定流收敛到下限、抖动流抬升、限速 2 ms/s、不来回震荡。"""
    jb = JitterBuffer()
    need((JitterBuffer.ADAPT_MIN_MS, JitterBuffer.ADAPT_MAX_MS) == (60.0, 200.0),
         "自适应上下限不对")
    t = 0.0
    deltas, trail = [], []
    for _ in range(1500):                       # 15 s 稳定 10 ms 到达
        t += 0.010
        before = jb.target_ms
        jb.push(np.zeros((FRAME_SAMPLES, CHANNELS), np.int16), now=t)
        jb.pull(FRAME_SAMPLES)
        deltas.append(abs(jb.target_ms - before))
        trail.append(jb.target_ms)
    low = jb.target_ms
    need(low <= 65.0, "稳定流没收敛到下限附近：%.1f ms" % low)
    need(max(trail[-300:]) - min(trail[-300:]) < 0.5,
         "稳定后目标还在动：%r" % (trail[-4:],))

    t2 = t
    hi_trail = []
    for i in range(1500):                       # 15 s 抖动流：4 / 16 ms 交替
        t2 += 0.004 if i % 2 == 0 else 0.016
        before = jb.target_ms
        jb.push(np.zeros((FRAME_SAMPLES, CHANNELS), np.int16), now=t2)
        jb.pull(FRAME_SAMPLES)
        deltas.append(abs(jb.target_ms - before))
        hi_trail.append(jb.target_ms)
    need(jb.target_ms > low + 8.0,
         "抖动流没抬目标：%.1f -> %.1f ms" % (low, jb.target_ms))
    need(jb.target_ms <= JitterBuffer.ADAPT_MAX_MS + 0.01,
         "超过上限：%.1f ms" % jb.target_ms)
    need(max(deltas) <= JitterBuffer.SLEW_MS_PER_S + 1e-6,
         "单次调整 %.3f ms 超过限速 %.1f" % (max(deltas), JitterBuffer.SLEW_MS_PER_S))
    downs = sum(1 for a, b in zip(hi_trail, hi_trail[1:]) if b < a - 1e-9)
    need(downs == 0, "抬升过程中出现 %d 次反向调整（震荡）" % downs)
    need(jb.stats()["target_ms"] == round(jb.target_ms, 1), "stats 没暴露 target_ms")
    return ("稳定流降到 %.0f ms，抖动流抬到 %.0f ms，最大单步 %.2f ms，"
            "反向调整 0 次" % (low, jb.target_ms, max(deltas)))


def t_concealment():
    """17 丢帧隐藏：优先基音外推（v1.2），找不到周期才退回 5 ms 淡出。"""
    jb = JitterBuffer()
    sig = tone(440.0, 0.25)                     # 25 帧 440 Hz
    ref = []
    for i in range(5):
        jb.push(sig[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES])
        ref.append(jb.pull(FRAME_SAMPLES))
    need(jb.concealed == 0, "还没欠载就计了 concealed=%d" % jb.concealed)

    gap = jb.pull(FRAME_SAMPLES)                # 缓冲已空 -> 欠载
    need(jb.underloads == 1, "underloads=%d" % jb.underloads)
    need(jb.concealed == 1, "concealed=%d（一次欠载只该记一次）" % jb.concealed)
    tail = ref[-1][-1].astype(np.int32)
    jump_out = int(np.max(np.abs(gap[0].astype(np.int32) - tail)))
    need(jump_out <= 600, "外推接缝跳变 %d" % jump_out)
    # v1.2：440 Hz 纯音应被识别出基音并继续发声，而不是直接衰减成静音
    need(jb.extrapolated == 1, "extrapolated=%d（应走外推）" % jb.extrapolated)
    need(380.0 <= jb.pitch_hz <= 500.0, "基音估计 %.1f Hz，期望 ~440" % jb.pitch_hz)
    amp = int(np.max(np.abs(gap[max(0, len(gap) - 48):])))
    need(amp > 500, "外推 10 ms 后幅度只剩 %d（应保留基频能量）" % amp)

    # 长丢包要逐渐衰减，不能一直"嗡嗡"重复
    long_gap = np.vstack([jb.pull(FRAME_SAMPLES) for _ in range(3)])
    early = float(np.max(np.abs(long_gap[0])))
    late = float(np.max(np.abs(long_gap[-1])))
    need(late < early * 0.9, "长丢包没有衰减：early %.0f late %.0f" % (early, late))

    for i in range(2):                          # 数据恢复
        jb.push(sig[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES])
    back = jb.pull(FRAME_SAMPLES)
    jump_in = int(np.max(np.abs(back[0].astype(np.int32) - long_gap[-1].astype(np.int32))))
    need(jump_in <= 900, "淡入接缝跳变 %d" % jump_in)
    need(int(np.max(np.abs(back))) > 3000, "淡入后没有恢复满幅：%d"
         % int(np.max(np.abs(back))))
    need(jb.concealed == 1, "恢复后又重复计 concealed")
    return ("外推接缝 %d，基音 %.1f Hz，10 ms 后幅度 %d，淡入接缝 %d，concealed=%d"
            % (jump_out, jb.pitch_hz, amp, jump_in, jb.concealed))


def t_mode_profiles():
    """24 延迟档位：low/stable 各自夹住目标、切换不跳变、统计字段齐全。"""
    jb = JitterBuffer()
    need(jb.mode == "auto", "默认档位应是 auto，实际 %s" % jb.mode)
    need(jb.set_mode("low") == "low", "set_mode 返回错")
    need((jb.min_ms, jb.max_ms) == JitterBuffer.MODE_PROFILES["low"], "low 档上下限错")
    need(jb.set_mode("stable") == "stable", "切 stable 失败")
    need((jb.min_ms, jb.max_ms) == JitterBuffer.MODE_PROFILES["stable"], "stable 档上下限错")
    need(jb.set_mode("nonsense") == "auto", "非法档位应回落到 auto")

    # low 档跑一段带抖动的流：目标不许越过 90 ms，且单步不超过 SLEW
    jb = JitterBuffer(mode="low")
    jb.prefill_to(jb.target_ms)
    trail = []
    rng = np.random.default_rng(5)
    for i in range(600):
        delay = float(rng.uniform(0.0, 0.030))
        time.sleep(0)
        jb.push(np.zeros((FRAME_SAMPLES, CHANNELS), np.int16), now=100.0 + i * 0.01 + delay)
        jb.pull(FRAME_SAMPLES)
        trail.append(jb.target_ms)
    need(max(trail) <= JitterBuffer.MODE_PROFILES["low"][1] + 0.01,
         "low 档目标越过上限：%.1f" % max(trail))
    need(min(trail) >= JitterBuffer.MODE_PROFILES["low"][0] - 0.01,
         "low 档目标低于下限：%.1f" % min(trail))
    deltas = [abs(b - a) for a, b in zip(trail, trail[1:])]
    need(max(deltas) <= JitterBuffer.SLEW_MS_PER_S + 1e-6,
         "单步 %.2f ms 超过限速" % max(deltas))

    st = jb.stats()
    for key in ("mode", "mode_min_ms", "mode_max_ms", "pitch_hz", "extrapolated"):
        need(key in st, "stats 缺少 %s" % key)
    return "auto/low/stable 上下限正确，low 档目标 %.0f~%.0f ms，非法档位回落 auto" % (
        min(trail), max(trail))


def t_pitch_extrapolation():
    """25 基音外推：200 Hz 纯音丢 20 ms 仍保持基频与能量，静音输入退回淡出。"""
    jb = JitterBuffer(target_ms=60.0)
    sig = tone(200.0, 0.20)
    for i in range(6):
        jb.push(sig[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES])
        jb.pull(FRAME_SAMPLES)
    gap = np.vstack([jb.pull(FRAME_SAMPLES) for _ in range(2)])     # 20 ms 空洞
    need(jb.extrapolated >= 1, "没走外推（extrapolated=%d）" % jb.extrapolated)
    need(180.0 <= jb.pitch_hz <= 225.0, "基音 %.1f Hz，期望 ~200" % jb.pitch_hz)

    mono = gap[:, 0].astype(np.float64)
    need(np.max(np.abs(mono[: FRAME_SAMPLES])) > 500, "第一块外推幅度太低：%.0f"
         % np.max(np.abs(mono[: FRAME_SAMPLES])))
    # 过零率应接近 200 Hz（每周期 2 次过零）
    zc = int(np.sum(np.diff(np.signbit(mono[: FRAME_SAMPLES]))))
    expect = 2 * 200.0 * (FRAME_SAMPLES / float(SAMPLE_RATE))
    need(abs(zc - expect) <= expect * 0.6,
         "外推段过零 %d 次，期望约 %.0f（基频没保持）" % (zc, expect))

    # 静音输入：不该外推出满幅噪声，只能保持静音
    jb2 = JitterBuffer()
    for _ in range(4):
        jb2.push(np.zeros((FRAME_SAMPLES, CHANNELS), np.int16))
        jb2.pull(FRAME_SAMPLES)
    quiet = jb2.pull(FRAME_SAMPLES)
    need(int(np.max(np.abs(quiet))) == 0, "静音输入外推出 %d" % int(np.max(np.abs(quiet))))
    return "200 Hz 音丢 20 ms：基音 %.1f Hz，过零 %d 次，幅度保持，静音输入不外推" % (
        jb.pitch_hz, zc)


def t_drift_pm05():
    """18 时钟漂移 ±0.5%：ppm 估得准、600 s 不出现溢出、深度有界。"""
    lines = []
    for ppm in (5000.0, -5000.0):
        jb = JitterBuffer()
        jb.prefill_to(jb.target_ms)
        rate = 100.0 * (1.0 + ppm / 1e6)
        t_push = 0.0
        t = 0.0
        max_depth = 0.0
        for _ in range(30000):                  # 600 s，20 ms 一步
            t += 0.020
            while t_push <= t:
                t_push += 1.0 / rate
                jb.push(np.zeros((FRAME_SAMPLES, CHANNELS), np.int16), now=t_push)
            jb.pull(2 * FRAME_SAMPLES)
            max_depth = max(max_depth, jb.depth_ms)
        need(abs(jb.drift_ppm - ppm) < 0.15 * abs(ppm),
             "ppm 估计 %.0f，期望 %.0f" % (jb.drift_ppm, ppm))
        need(jb.overflows == 0, "%+.0f ppm 出现 %d 次溢出" % (ppm, jb.overflows))
        need(max_depth <= 260.0, "%+.0f ppm 深度涨到 %.0f ms" % (ppm, max_depth))
        lines.append("%+.0fppm→估%+.0f/溢出%d/峰值%.0fms/修剪%d"
                     % (ppm, jb.drift_ppm, jb.overflows, max_depth, jb.trims))
    return "；".join(lines)


def t_spectrum_bands():
    """19 频谱：64 段对数频段，440 Hz 落在其主瓣段，峰值保持 1 s 衰减约 6 dB。"""
    s = SpectrumAnalyzer()
    need(s.bands == 64, "段数 %d" % s.bands)
    need(abs(s.edges[0] - 40.0) < 1e-6 and abs(s.edges[-1] - 16000.0) < 1e-6,
         "频段范围 %r" % (s.edges[[0, -1]],))
    need(np.all(np.diff(s.edges) > 0), "频段边界不是单调递增")

    mono = tone(440.0, 0.2, 0.5)[:, 0]
    db, peaks = s.analyze(mono, 0.04)
    b = s.band_of(440.0)
    need(int(np.argmax(db)) == b,
         "440 Hz 最强段是 %d，期望 %d" % (int(np.argmax(db)), b))
    need(s.edges[b] <= 440.0 < s.edges[b + 1],
         "440 Hz 不在第 %d 段 [%.0f, %.0f)" % (b, s.edges[b], s.edges[b + 1]))
    need(db[b] > -20.0, "440 Hz 段电平太低 %.1f dB" % db[b])
    need(db.shape == (64,) and peaks.shape == (64,), "输出形状 %r/%r"
         % (db.shape, peaks.shape))

    db2, pk2 = s.analyze(np.zeros(2048, np.int16), 1.0)
    want = peaks[b] - 6.0
    need(abs(pk2[b] - want) < 1.0, "1 s 后峰值 %.1f，期望 %.1f" % (pk2[b], want))
    need(pk2[b] >= db2[b] - 1e-9, "峰值保持低于当前值")
    return "64 段 40 Hz~16 kHz，440 Hz 落在第 %d 段 [%.0f,%.0f)，峰值保持 %.1f→%.1f dB" % (
        b, s.edges[b], s.edges[b + 1], peaks[b], pk2[b])


def t_waveform_columns():
    """20 波形 min/max：每列的极值与输入完全一致；环形缓冲读回也一致。"""
    width, k = 8, 50
    vals = np.array([0, 100, -100, 3000, -3000, 12345, -23456, 32767], np.int32)
    x = np.repeat(vals, k).astype(np.int16)
    mins, maxs = WaveformView.columns(x, width)
    need(mins.shape == (width,) and maxs.shape == (width,), "列数 %r" % (mins.shape,))
    need(np.array_equal(mins, vals) and np.array_equal(maxs, vals),
         "min/max 不对：%r / %r" % (mins.tolist(), maxs.tolist()))

    stereo = np.stack([np.full(96, -1000, np.int16), np.full(96, 3000, np.int16)], 1)
    mn, mx = WaveformView.columns(stereo, 12)
    need(np.all(mn == 1000) and np.all(mx == 1000),
         "立体声混单声道不对：%r/%r" % (mn.tolist()[:3], mx.tolist()[:3]))

    tap = RingTap(seconds=0.1)
    chunk = np.stack([np.arange(480, dtype=np.int16), np.arange(480, dtype=np.int16)], 1)
    tap.write(chunk)
    tap.write(chunk)
    got = tap.read_last(480)
    need(np.array_equal(got, np.arange(480, dtype=np.int16)), "环形缓冲内容不对")
    need(np.array_equal(tap.read_last(960)[:480], np.arange(480, dtype=np.int16)),
         "环形缓冲跨回绕读取不对")
    short = tap.read_last(4800)
    need(short.shape == (4800,) and int(np.max(np.abs(short[:3840]))) == 0,
         "环形缓冲不足时没补 0")
    return "8 列 min/max 精确，单声道混合 =1000，环回读取一致，未满处补 0"


def t_recorder_roundtrip():
    """21 录音：写出的 WAV 与"播放内容"逐字节一致，且入队时已拷贝。"""
    rec = WavRecorder()
    path = rec.start(os.path.join(TMP, "rec_roundtrip.wav"))
    src = tone(1000.0, 0.3, 0.4)
    blocks = [src[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES].copy()
              for i in range(src.shape[0] // FRAME_SAMPLES)]
    for b in blocks:
        rec.feed(b)
    # 喂进去之后改动原数组：不能影响已写内容（证明 feed 做了拷贝）
    for b in blocks:
        b.fill(12345)
    info = rec.stop()
    need(info["frames"] == len(blocks) * FRAME_SAMPLES,
         "记录帧数 %d" % info["frames"])
    with wave.open(path, "rb") as w:
        need((w.getnchannels(), w.getsampwidth(), w.getframerate()) == (2, 2, 48000),
             "WAV 头 (ch,bytes,rate) = %r"
             % ((w.getnchannels(), w.getsampwidth(), w.getframerate()),))
        raw = w.readframes(w.getnframes())
    need(raw == src.astype("<i2").tobytes(), "录音与播放内容不一致")
    need(abs(info["seconds"] - src.shape[0] / 48000.0) < 1e-3,
         "秒数 %.3f" % info["seconds"])
    return "%.2f s / %d 帧 / %d 字节，逐字节一致（修改源数组不影响）" % (
        info["seconds"], info["frames"], info["bytes"])


def t_control_packets():
    """22 控制面：b"CTL"+命令+参数 的字节精确，音频解析器会忽略，实际可发。"""
    cases = [
        ("MUTE", None, b"CTL\x01"),
        ("UNMUTE", None, b"CTL\x02"),
        ("GAIN+", None, b"CTL\x03"),
        ("GAIN-", None, b"CTL\x04"),
        ("MODE", 2, b"CTL\x05\x02"),
        ("TEXT", "hi 板", b"CTL\x06" + "hi 板".encode("utf-8")),
    ]
    for cmd, arg, want in cases:
        got = pack_control(cmd, arg)
        need(got == want, "%s -> %s，期望 %s" % (cmd, got.hex(), want.hex()))
        code, back = unpack_control(got)
        need(code == got[3], "%s 命令码 %d" % (cmd, code))
        if cmd == "TEXT":
            need(back == "hi 板", "TEXT 解码 %r" % (back,))
        elif cmd == "MODE":
            need(back == 2, "MODE 参数 %r" % (back,))

    try:
        unpack_packet(pack_control("MUTE"))
        raise AssertionError("CTL 包不该被当成音频包")
    except ProtocolError:
        pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    port = sock.getsockname()[1]
    ctl = BoardControl(dest="127.0.0.1", port=port)
    info = ctl.send("MODE", 3)
    data, _ = sock.recvfrom(1024)
    ctl.close()
    sock.close()
    need(data == b"CTL\x05\x03", "线上字节 %s" % data.hex())
    need(info["hex"] == "43 54 4C 05 03", "回显 hex %r" % info["hex"])
    need(info["cmd"] == "MODE 3", "回显命令名 %r" % info["cmd"])
    return "6 条命令字节精确，hex=%s，音频解析器忽略 CTL 包" % info["hex"]


def t_fw_ctl():
    """26 固件控制面：单字节、幂等、走 50507（真机唯一认的协议）。"""
    cases = [
        ("bypass", None, b"0"), ("gate", None, b"2"), ("rnnoise", None, b"1"),
        ("noise", 0, b"a"), ("noise", 300, b"b"),
        ("noise", 800, b"c"), ("noise", 1500, b"d"),
        ("tone", None, b"t"),
    ]
    for cmd, arg, want in cases:
        got = fw_ctl_frame(cmd, arg)
        need(got == want, "%s(%r) -> %s，期望 %s" % (cmd, arg, got, want))
        need(len(got) == 1, "%s 不是单字节：%r" % (cmd, got))

    for bad in ("mute", "MODE", "gain+"):
        try:
            fw_ctl_frame(bad)
            raise AssertionError("%r 应该报错（固件没有这个命令）" % bad)
        except ValueError:
            pass
    try:
        fw_ctl_frame("noise", 500)
        raise AssertionError("底噪 500 应该报错（固件只有 0/300/800/1500）")
    except ValueError:
        pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    port = sock.getsockname()[1]
    ctl = BoardCtl(dest="127.0.0.1", port=port)
    info = ctl.dsp("gate")
    data, _ = sock.recvfrom(64)
    ctl.noise(800)
    data2, _ = sock.recvfrom(64)
    ctl.close()
    sock.close()
    need(data == b"2", "线上字节 %r，期望 b'2'" % data)
    need(data2 == b"c", "底噪线上字节 %r，期望 b'c'" % data2)
    need(info["hex"] == "32" and info["char"] == "2", "回显 %r" % (info,))
    need(BoardCtl().port == FW_CTL_PORT == 50507, "默认端口必须是 50507")
    return "8 条命令单字节精确，默认端口 50507，未知命令报错"


def t_gain_ab():
    """23 A/B 增益：1.0x 不改动，2.0x 精确放大并饱和到 int16 边界。"""
    x = np.full((FRAME_SAMPLES, CHANNELS), 1000, np.int16)
    need(np.array_equal(apply_gain(x, 1.0), x), "1.0x 改动了样本")
    y = apply_gain(x, 2.0)
    need(y.dtype == np.int16 and np.all(y == 2000), "2.0x 结果 %r" % (y.ravel()[:3],))
    loud = np.full((FRAME_SAMPLES, CHANNELS), 30000, np.int16)
    need(np.all(apply_gain(loud, 2.0) == 32767), "2.0x 没饱和到 32767")
    need(np.all(apply_gain(-loud, 2.0) == -32768), "-2.0x 没饱和到 -32768")
    need(np.array_equal(apply_gain(loud, 1.0), loud), "1.0x 不该饱和")
    return "1.0x 原样 / 2.0x 2000 / 30000→32767（负向 -32768）"


# ------------------------------------------------------------------ main

def main() -> int:
    global TMP
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    TMP = tempfile.mkdtemp(prefix="udp_audio_selftest_")

    print("=" * 78)
    print(" AU/UDP 音频协议自测（不需要板子、不打开声卡）")
    print(" python %s / numpy %s" % (sys.version.split()[0], np.__version__))
    print("=" * 78)

    cases = [
        ("01 报文布局", t_header_layout),
        ("02 一帧拆两包", t_pack_frame),
        ("03 UDP 回环重组", t_loopback_udp),
        ("04 seq 丢帧与回绕", t_seq_loss),
        ("05 乱序与重复包", t_reorder_dup),
        ("06 协议容错", t_protocol_tolerance),
        ("07 WAV 往返", t_wav_roundtrip),
        ("08 WAV 转码 24k 单→48k 立体声", t_wav_convert_mono_24k),
        ("09 抖动缓冲欠载", t_jitter_underflow),
        ("10 抖动缓冲溢出", t_jitter_overflow),
        ("11 时钟漂移 0.1%", t_jitter_drift),
        ("12 电平 dBFS", t_dbfs),
        ("13 测试音频率", t_tone_freq),
        ("14 MicReceiver 线程", t_mic_receiver_thread),
        ("15 发送节拍", t_sender_pacing),
        ("16 自适应目标深度", t_adaptive_target),
        ("17 丢帧隐藏淡出淡入", t_concealment),
        ("18 漂移 ±0.5% × 600 s", t_drift_pm05),
        ("19 频谱 64 段与峰值", t_spectrum_bands),
        ("20 波形 min/max 与环回", t_waveform_columns),
        ("21 录音 WAV 逐位一致", t_recorder_roundtrip),
        ("22 控制命令报文", t_control_packets),
        ("23 A/B 增益", t_gain_ab),
        ("24 延迟档位 auto/low/stable", t_mode_profiles),
        ("25 基音外推丢包隐藏", t_pitch_extrapolation),
        ("26 固件控制面单字节", t_fw_ctl),
    ]
    for name, fn in cases:
        check(name, fn)

    t_all = 0.0
    passed = failed = 0
    for name, ok, detail, dt in RESULTS:
        t_all += dt
        if ok:
            passed += 1
            tag = "[PASS]"
        else:
            failed += 1
            tag = "[FAIL]"
        print("%s %-30s %s" % (tag, name, detail))
    print("-" * 78)
    print(" %d passed, %d failed  (%.2f s)  临时目录 %s"
          % (passed, failed, t_all, TMP))
    print(" RESULT: %s" % ("ALL OK" if failed == 0 else "FAILED"))
    print("=" * 78)

    try:
        shutil.rmtree(TMP, ignore_errors=True)
    except Exception:
        pass
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
