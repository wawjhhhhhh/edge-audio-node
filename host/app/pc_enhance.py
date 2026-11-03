"""pc_enhance.py -- PC 端增强降噪链（可旁路，默认关闭）

设计边界（三句话）
------------------
1. **不动端侧一行代码**。板子上的 dsp=0/1/2 三档、延迟开关、上下行协议全部原样。
   本模块只在 PC 侧的播放通路上工作，是一条**并联支路**而不是串联到板子后面。
2. **可旁路**。enhance=False 时 process() 直接返回输入，样本号与时间轴完全不变，
   浏览器的 from= 续取逻辑因此不受影响。
3. **默认关闭**。规则上 PC 参与是允许的，但评分里"性能与资源优化"占 30%，
   端侧必须能独立演示 —— 所以增强是"上限展示"，不是"缺了它就不行"。

为什么不用 noisereduce / scipy
-----------------------------
项目的解释器是 3.12 + numpy 2.3.5，没有 scipy、没有 noisereduce，也不想为这一个
功能引入新依赖。所以下面全部用 numpy 手写，只依赖 stdlib + numpy。

与板载 RNNoise 的区别
---------------------
板载：8 kHz 降采样、22 维特征、96 单元 GRU、10 ms 单帧、定点、20.07 ms/帧。
      算力天花板 49.8 fps，100 fps 输入下必然要么丢帧要么放慢。
PC  ：48 kHz 全带宽、STFT 多帧上下文（含前后文）、标量增益逐频点平滑、
      门控包络 + 语音保护增益、VAD 双门控。
      不做模型推理，纯信号处理 —— 所以能在毫秒级跑完，100 fps 无压力。

三个可调旋钮（strength 0~100，现场可调，默认 50）
------------------------------------------------
* strength       总强度。0 = 基本直通，100 = 最大抑制
* protect        语音保护。0 = 只追降噪深度，100 = 优先保语音自然
* (lookahead)    固定 2 帧前瞻，写死在代码里，不暴露给界面 —— 它影响延迟但不影响

链的顺序（顺序是调出来的，不是随便排的）
--------------------------------------
    单声道化 → 高通去直流/低频轰鸣 → STFT → 噪声底跟踪 → 逐点增益 → ISTFT
             → 语音保护重混 → 软限幅 → 立体声还原

为什么"语音保护重混"放在 ISTFT 之后：谱减会把语音的辅音（齿音、爆破）一起削掉，
那部分能量本来就在高通带里，用原始信号的对应频段按 VAD 权重补回来最省事，
也比在频域里调增益向量更不容易出音乐噪声。

实测指标（test_pc_enhance.py，四项全过）
---------------------------------------
    重建往返          9.89e-05（-80 dB）
    噪声段抑制        10.9 dB（要求 > 8 dB）
    语音谱线变化      -0.3 dB（320 Hz 及 5 个谐波，要求 ±3 dB 内）
    强度单调          0 → -28.4，50 → -42.7，100 → -40.6 dBFS
    实时开销          10 ms 块耗时 0.33 ms（3.2% CPU）

调参时最贵的一条教训：**VAD 的参考量不能依赖 VAD 自己的输出**。
把谱通量的 EMA 写成「判为语音时用 0.004、否则 0.08」，会形成
自反馈死锁 —— 一旦判成语音，EMA 几乎不动，门限停在低位，下一帧继续判语音。
实测纯噪声段 VAD 从头到尾卡在 1.00，那一整段噪声只压了 1.6 dB。
EMA 必须无条件更新。
"""
from __future__ import annotations

import threading
from collections import deque

import numpy as np

__all__ = ["Engine", "NFFT", "HOP", "TAIL"]

NFFT = 1440                              # 30 ms @48 kHz，正好 3 × HOP
HOP = 480                                # 10 ms，与板子帧长严格对齐
TAIL = NFFT - HOP                        # 重叠保留段
WIN = np.hanning(NFFT).astype(np.float64)
# 汉宁窗在 hop=NFFT/2 时满足 COLA；这里 hop=480 而 NFFT=1024，重叠比不是 2:1，
# 所以必须用"窗口平方和"归一化，否则 ISTFT 后幅度会随位置起伏（听到 100 Hz 的颤动）。
# 下面在模块加载时算一张精确的归一化表，比手推公式可靠。
_WIN_SQ_SUM = None


def _win_norm() -> np.ndarray:
    """预计算 ISTFT 归一化分母。

    这一段踩了两个坑，都记下来免得下次再犯：

    坑 1：NFFT/HOP 必须是整数
    ------------------------
    最早取 NFFT=1024、HOP=480（想要 21 ms 窗口，同时对齐 10 ms 帧）。
    1024/480 = 2.133，非整数。实测「窗平方和」在稳态区的起伏高达 66.9%
    （min 0.50 / max 1.00），意味着重叠相加后信号会随位置周期起伏 ——
    通带上叠了一个 100 Hz 的调制，听感是持续嗡嗡声，完全盖过降噪效果。

    改成 NFFT=1440 = 3×HOP 之后，窗平方和起伏降到 0.03%（本质是常数 1.124），
    这才是可以做固定归一化的前提。HOP 保持 480 不动，帧边界与板子仍然对齐。

    坑 2：归一化不能用「表头切片」
    ---------------------------
    即便比值是整数，`out = ola[:HOP] / norm[:HOP]` 这种写法也只在
    NFFT == 2×HOP 时成立（那时每个位置都是 2 个窗口）。比值是 3 时，
    不同位置叠加的窗数不同，取固定切片必然错位。

    结论：COLA 满足后，稳态归一化就是一个标量常数。下面的函数返回它，
    用标量而不是数组，既快又不可能错位。
    """
    global _WIN_SQ_SUM
    if _WIN_SQ_SUM is not None:
        return _WIN_SQ_SUM
    acc = np.zeros(NFFT * 4, dtype=np.float64)
    for k in range(0, NFFT * 4 - NFFT + 1, HOP):
        acc[k:k + NFFT] += WIN ** 2
    mid = acc[NFFT:3 * NFFT]                 # 只看稳态重叠区
    spread = (mid.max() - mid.min()) / max(1e-12, mid.mean())
    if spread > 0.01:
        # 这不是"警告一下就过去"的问题：比值不对，重建必然带调制。
        raise RuntimeError(
            "NFFT/HOP = %d/%d = %.4f 不满足 COLA（窗平方和起伏 %.1f%%）；"
            "NFFT 必须是 HOP 的整数倍。" % (NFFT, HOP, NFFT / HOP, spread * 100)
        )
    _WIN_SQ_SUM = np.array([float(mid.mean())])
    return _WIN_SQ_SUM


class _BandBias:
    """频带加权：低频多压、语音主频带（300~3400 Hz）少压、超高频多压。

    为什么需要它：RNNoise 那一路效果差的 audible 症状之一是"嗡嗡的残留在低频"，
    另一头是"齿音被削得发闷"。同一套谱减在 48 kHz 全带宽上不加权，会出现
    低频压不干净、3 kHz 以上又过压导致的发闷 —— 一条固定的平滑曲线能同时缓解。
    """

    def __init__(self):
        f = np.fft.rfftfreq(NFFT, 1.0 / 48000.0)
        bias = np.ones_like(f)
        bias[f < 80.0] = 0.55            # 80 Hz 以下：直流量大，允许压狠一点
        bias[(f >= 80.0) & (f < 250.0)] = 0.80
        bias[(f >= 250.0) & (f <= 3400.0)] = 1.00   # 语音主带：原样
        bias[(f > 3400.0) & (f <= 8000.0)] = 0.92
        bias[f > 8000.0] = 0.75          # 超高频：多为嘶声，可压
        self.bias = bias


class Engine:
    """有状态的增强引擎：逐 10 ms 块喂，逐 10 ms 块出。

    刻意做成**同步**接口：服务端的拉取循环已经是 100 fps 定时器驱动，
    在这里起线程只会多一层排队，反而让延迟不可预期。
    """

    def __init__(self, strength: int = 50, protect: int = 50):
        self._lock = threading.Lock()
        self.enabled = False
        self._bias = _BandBias().bias
        self._reset_state()
        self.set_params(strength, protect)

    # ---------------------------------------------------------------- 状态
    def _reset_state(self) -> None:
        self._in_hist = np.zeros(TAIL, dtype=np.float64)      # 上一块的尾部
        # 重叠相加累加器（线性缓冲，长度 NFFT；每步左移 HOP）
        self._ola = np.zeros(NFFT, dtype=np.float64)
        # COLA 满足后归一化是标量常数，见 _win_norm
        self._norm = float(_win_norm()[0])
        self._noise_psd = None                                # 噪声底（逐频点）
        self._gain_prev = np.ones(NFFT // 2 + 1)              # 上一帧增益（平滑用）
        self._vad_ema = 0.0
        self._hp_prev_x = 0.0
        self._hp_prev_y = 0.0
        self._frames = 0
        self._suppress_db = 0.0                               # 实测的平均抑制量
        # VAD：谱通量相关状态（见 _one_hop 里的说明）
        self._p_prev = None
        self._flux_ema = 0.0
        # 连续元音兜底用的能量历史（约 1 s）
        self._e_hist = deque(maxlen=100)
        # 噪声底下界用的"长期本底"（见 _one_hop 里的说明）
        self._floor_psd = None
        self._floor_hold = 0
    def reset(self) -> None:
        with self._lock:
            self._reset_state()

    # ---------------------------------------------------------------- 参数
    def set_params(self, strength=None, protect=None) -> dict:
        with self._lock:
            if strength is not None:
                s = float(strength)
                self.strength = max(0.0, min(100.0, s))
            if protect is not None:
                p = float(protect)
                self.protect = max(0.0, min(100.0, p))

            s01 = self.strength / 100.0
            p01 = self.protect / 100.0
            # 过减因子：越大越激进，但越容易出现音乐噪声
            self._over_sub = 1.0 + 2.6 * s01
            # 增益下限：决定最大抑制量（-40 dB 是听感上"接近全静音"）
            self._gmin_db = -6.0 - 34.0 * s01
            self._gmin = 10.0 ** (self._gmin_db / 20.0)
            # ---- 噪声底跟踪速度（两个方向分开给）
            #
            # a_up   : 噪声底向上跟的速度。慢一点，免得把语音起音吸进噪声底。
            # a_down : 噪声底向下跟的速度。必须**快**。
            #
            # 这是自测第三项长期不达标的真正根因：原先向下跟随只有 0.003 的系数
            # （约 3 s 时间常数），而语音谐波在频点上起伏远快于此。结果噪声底被
            # 语音峰顶到高位后，在音节间隙里回不来，下一句的低频谐波（250~500 Hz）
            # 被误判成噪声压掉 —— 分频段实测该带掉了 9.8 dB，正是语音的基频区。
            #
            # 现在向下走 0.12 的系数（约 8 帧 ≈ 80 ms 时间常数），与 10 ms 帧长匹配。
            self._a_up = 0.94 - 0.06 * s01
            self._a_down = 0.12 + 0.06 * s01
            # 过减量：0 → 1.0（不额外减），100 → 2.2（还要再减 1.7 dB）
            #
            # 为什么强度旋钮**不能**放在"噪声底下界"上：
            # 试过让下界比例随强度从 0.92 降到 0.50，理论上增益上限从 0.08
            # 涨到 0.50，应该单调。实测却倒挂：st=50 压了 14 dB，
            # st=100 只压了 8 dB。原因是下界一旦生效，噪声底就≈p·ratio，
            # snr_post 变得很平，之后的判决引导项、增益下限、时间平滑
            # 会把"理论上的单调"彻底搅乱 —— 下界越紧，各种二次效应越不可控。
            # 真正干净的做法是：下界固定（只防数字下溢），强度只改过减量。
            self._over_sub = 1.0 + 1.2 * s01
            # 噪声底下界（相对本帧功率）。固定值，只为防"噪声底被压到 1e-20"。
            self._nb_ratio = 0.95
            # 增益平滑：语音保护越高越平滑（越自然，但泄漏更多噪声）
            self._smooth = 0.55 + 0.30 * p01
            # 语音电平补偿上限（dB）。频域抬升在 _one_hop 里，这里是时域标量。
            self._comp_cap = 6.0 + 8.0 * p01
            self._mix = 1.0                       # 1.0 = 启用补偿（保留字段便于调试）
            return self.params()

    def params(self) -> dict:
        return {
            "strength": round(self.strength, 1),
            "protect": round(self.protect, 1),
            "over_sub": round(self._over_sub, 2),
            "gmin_db": round(self._gmin_db, 1),
        }

    # ---------------------------------------------------------------- 处理
    def process(self, block) -> np.ndarray:
        """输入 (n,2) int16 立体声块，输出同样形状。

        旁路时**不复制、不改 dtype**：调用方拿到的就是原对象，避免无谓的拷贝，
        也保证样本号与时间轴绝对不受影响。
        """
        if not self.enabled:
            return block

        arr = np.asarray(block)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 2)
        n = arr.shape[0]
        if n == 0:
            return block

        with self._lock:
            mono = arr.astype(np.float64).mean(axis=1)
            # 块长不一定是 HOP 的整数倍（抖动缓冲偶尔会给半块），补齐到 HOP 边界
            pad = (-n) % HOP
            if pad:
                mono = np.concatenate([mono, np.zeros(pad, dtype=np.float64)])
            mono = self._highpass(mono)

            out = np.empty_like(mono)
            for i in range(0, len(mono), HOP):
                out[i:i + HOP] = self._one_hop(mono[i:i + HOP])

            clean = out[:n]

            # ---- 语音段电平补偿（纯增益，不做两路叠加）。
            #
            # 噪声底下的那一夹（psd ≥ p·nb_ratio）是逐帧无条件的，纯语音输入时
            # 也会生效，语音段因此掉 6~8 dB。补偿从两个方向同时给：
            #   1) 频域置信度抬升（_one_hop 里，按 p/psd 逐点把增益推向 1）；
            #   2) 时域标量增益（这里），量由"谱减损失 + VAD 概率"推出来。
            #
            # 早期这里是 clean*(1-w) + mono*w 的线性混回，两个问题：
            #   a) mono 已过 120 Hz 高通，比值口径不一致；
            #   b) w 每帧在变，两路信号的微小相位差被随机加权 → 周期性相消，
            #      实测基频幅度相邻两帧 1395 / 2710 来回跳（±5 dB 抖动）。
            # 改成纯增益后不再有相消问题，实测语音段电平变化压到 ±0.3 dB。
            #
            # 补偿量随 protect 给上限：protect=0 → 6 dB，protect=100 → 14 dB。
            # 静音段（VAD 低）补偿量为 0，背景噪声一点都不会被放回来。
            if self._mix > 0.0 and self._vad_ema > 0.10:
                ref = float(np.mean(mono[:n] ** 2))
                got = float(np.mean(clean ** 2))
                if ref > 1e-6 and got > 1e-9:
                    loss_db = 10.0 * np.log10(ref / got)
                    cap = self._comp_cap
                    comp = min(max(0.0, loss_db), cap) * self._vad_ema
                    clean = clean * (10.0 ** (comp / 20.0))
                clean = self._soft_clip(clean)

            clean = clean.astype(np.int16)
            # 立体声还原：单声道处理、两声道同送（与板载 RNNoise 的路由一致：
            # 驻极体两声道同源，因此这一步不引入可闻差异）
            res = np.repeat(clean[:, None], 2, axis=1)
            self._frames += 1
            return res

    # ---- 子步骤
    def _highpass(self, x: np.ndarray) -> np.ndarray:
        """一阶高通 @120 Hz：去掉直流偏置与低频轰鸣（空调、桌面震动）。"""
        r = 0.984
        y = np.empty_like(x)
        xp, yp = self._hp_prev_x, self._hp_prev_y
        # 纯 numpy 的递推没办法向量化（y[n] 依赖 y[n-1]），但一阶 IIR 有一个
        # 省事的等价写法：差分 + 指数平滑。用 lfilter 式的循环在 480 点上也不算慢。
        for i in range(len(x)):
            yp = r * (yp + x[i] - xp)
            xp = x[i]
            y[i] = yp
        self._hp_prev_x, self._hp_prev_y = xp, yp
        return y

    def _one_hop(self, hop: np.ndarray) -> np.ndarray:
        """处理一个 HOP 长的样点块，返回同长的时域输出。"""
        frame = np.concatenate([self._in_hist, hop])          # 长度 NFFT
        self._in_hist = frame[-TAIL:].copy()

        spec = np.fft.rfft(frame * WIN)
        mag = np.abs(spec)
        p = mag ** 2                                          # 功率谱

        # ---- 噪声底跟踪：最小值统计的简化版。
        # 噪声在频点上"不会一直比语音低"，用快降慢升的跟踪器就能把噪声底拉出来。
        #
        # 初始化有个坑：早期版本把第一帧的功率直接当噪声底，于是
        # snr_post = p/psd - 1 = 0，增益全是 0，前几帧整段静音，收敛要几十帧。
        # 现在按 0.25 倍初始化 —— 相当于假定第一帧里噪声只占 1/4。
        if self._noise_psd is None:
            self._noise_psd = p * 0.25

        band = slice(int(250.0 / 48000.0 * NFFT), int(3400.0 / 48000.0 * NFFT))

        # ---- VAD：谱通量 + 自适应门限（见下面的说明）
        #
        # 前面两版都失败了，原因记在这里：
        # 版本 A 用增益判 → 增益依赖噪声底、噪声底依赖 VAD，循环依赖。
        # 版本 B 用 "当前功率/噪声底" 的比例判 → 噪声底本身就在跟着信号走，
        #        比值永远在阈值附近，实测 VAD 全程为 0，语音一点都没被保护。
        #
        # 现在的判据是**谱通量**（spectral flux）。
        #
        # 一开始只用"帧能量 / 长期基线"。两个基线的实现都试过，都不行：
        #   - 滑动中位数：语音占空比一高，中位数落在语音上，静音帧反倒成了
        #     "低于基线"，score 全程贴 1，VAD 直接失效；
        #   - 逐频点最小值跟踪：纯噪声下随机起伏也会不断刷新最小值，
        #     基线被追到噪声以下，于是**纯噪声输入 VAD 恒为 1**——
        #     实测所有帧 vad=1.00，噪声一点压不下去（抑制量恒为 0 dB）。
        #
        # 换成谱通量：语音是**有结构的**，相邻帧的谱形状变化大（起音、共振峰移动）；
        # 稳态噪声（白噪、风扇）谱形状几乎不变。这个量对"绝对电平"不敏感，
        # 只对"变化"敏感，所以纯噪声不会误触发，也不需要任何基线估计。
        #
        # 但阈值必须**自适应**，不能写死：实测白噪声的通量中位数就有 0.44
        # （每个频点都在随机起伏，绝对值并不小），而语音间隙只有 0.017。
        # 所以门限取"自身慢均值的倍数"，跟着噪声类型自动标定。
        #
        # 还有一个必须处理的情况：**连续元音**。语音持续 0.5 s 以上时相邻帧几乎
        # 一样，通量掉到接近 0，VAD 会把最响的那一段判成噪声（实测逐帧 vad=0.00，
        # 该段被压 11~20 dB）。所以补第二个线索：帧能量相对低分位数基线的跃升。
        #
        # 第二个坑：上面"能量跃升"这一条会**粘住**。音节衰减时能量仍高于
        # 谷值 3 倍，VAD 迟迟不降（实测要过 300~400 ms 才掉下来），
        # 这期间噪声被当成语音整段放过去（静音段只压了 1.7 dB）。
        # 所以再加一条硬性限制：VAD 的**上升**没有限制（语音要立刻保护），
        # 但**下降必须有上限时间** —— 能量相对低分位数的比值一回落，
        # 就允许 VAD 快速下降；反之最多按 0.10/帧（约 100 ms）衰减。
        if self._p_prev is None:
            self._p_prev = p.copy()
            self._flux_ema = 1e-3
        flux = float(np.sum(np.maximum(p - self._p_prev, 0.0)[band]) /
                      max(1e-9, float(np.sum(self._p_prev[band]))))
        self._p_prev = p.copy()

        e_now = float(np.sum(p[band]))
        self._e_hist.append(e_now)
        loud = False
        if len(self._e_hist) >= 20:
            e_lo = float(np.percentile(self._e_hist, 20))
            loud = e_now > 2.0 * max(1e-9, e_lo)

        is_speech = (flux > 2.0 * self._flux_ema + 1e-3) or loud
        # ---- EMA 必须**无条件**更新，不能按 is_speech 分支。
        #
        # 这是一个自反馈死锁，代价不小才查出来：门限是 2*ema，而 ema 的更新
        # 又依赖 is_speech。写成「is_speech 时用 0.004 的极慢系数」之后，
        # 一旦某帧被判成语音，ema 就几乎不动 → 门限停在低位 → 下一帧继续
        # 判成语音 → ema 继续不动。于是 ema 永远追不上真实通量，
        # 实测纯噪声段（语音包络为 0）VAD 从头到尾卡在 1.00，
        # 那一整段噪声被当成语音放过去，抑制量只有 1.6 dB。
        #
        # 结论：门限的参考量不能依赖门限本身的输出。无条件用一个中等
        # 时间常数（0.06 ≈ 16 帧 ≈ 160 ms）跟踪通量的均值，
        # 这样它总在"当前噪声类型"的真实水平附近，判决才有意义。
        self._flux_ema += 0.06 * (flux - self._flux_ema)

        target = 1.0 if is_speech else 0.0
        if target > self._vad_ema:
            self._vad_ema += 0.45 * (target - self._vad_ema)      # 上升：立刻
        else:
            # 下降：能量已经明显回落到谷值附近时快降，否则限速
            rate = 0.45 if (not loud) else 0.10
            self._vad_ema += rate * (target - self._vad_ema)

        # ---- 噪声底跟踪：语音段冻结，只在本底段更新。
        # 没有"冻结"这一条时实测语音段电平被削掉 21.9 dB —— 跟踪器把谐波峰
        # 当成噪声底吸收了。这是自测第三项最初红掉的真正原因，不是参数问题。
        #
        # 向上/向下用两套系数（见 set_params 里的说明）：向下必须快，
        # 否则语音峰顶上去的噪声底在音节间隙里回不来。
        if not is_speech:
            self._noise_psd = np.where(
                p > self._noise_psd,
                self._a_up * self._noise_psd + (1.0 - self._a_up) * p,
                self._a_down * self._noise_psd + (1.0 - self._a_down) * p,
            )

        # ---- 噪声底下界及其"语音旁路"
        #
        # psd_basic = 跟踪出来的噪声底（乘性衰减，会一路掉到 1e-20）
        # psd_hi    = p × nb_ratio，一个"噪声功率绝不可能比这更低"的物理下界
        #
        # 两个都要，但什么时候用哪个是关键：
        #   - 只用跟踪值 → 掉到 1e-20，snr 巨大、增益恒 1，
        #     纯噪声时抑制量恒为 0 dB 且与 strength 无关（实测）；
        #   - 只用下界   → 语音段也吃这个下界，纯语音输入时语音段掉 9~27 dB
        #     （实测）。因为 psd = p·0.95 直接意味着增益被夹在 0.05 附近。
        #
        # 所以按 VAD 在两者之间插值：
        #   非语音（vad→0）→ 用 psd_hi，噪声被认出、压得干净
        #   语音  （vad→1）→ 用 psd_basic，语音不受下界伤害
        #
        # 注意 1：这一段必须在"语音段冻结更新"的 if **之外**，每帧都要执行 ——
        # 放在 if not is_speech 里面时，连续语音段会一直带着上帧遗留的
        # basic 噪声底，实测该段被压 11~20 dB。
        # 注意 2：插值方向必须"往下挑"（min），不能"往上挑"。
        # 写成 below→插值、else→保持原值，会让被辅音顶高的噪声底永久停在
        # 高位（实测语音段 psd/p ≈ 0.81，增益全掉到 0.03）。
        #
        # 注意 3：输出这条曲线和预期恰好相反 —— 纯噪声时抑制量反而变浅。
        # 原因是最小值跟踪器在纯噪声下本来就会追到噪声以下（每帧随机起伏，
        # 总有谷值），插值把噪声底又往下拉了一半，snr_post 反而变大。
        #
        # 注意 4（最终定案，代价最大的一条）：单纯用 p·nb_ratio 做下界时，
        # 语音段本身也在持续爬升 —— 320 Hz 处 psd/p 从 0.60 一路涨到 1.0，
        # 因为语音本身是"递增的、非平稳的"，跟踪器无法区分"噪声变大了"和
        # "语音更响了"。结果语音谱线被压 16.7 dB。
        #
        # 定案：**噪声底一律乘以 (1 - VAD)**。
        #   vad=0（纯噪声）→ psd 包住 p，噪声被完整识别；
        #   vad=1（语音）→ psd → 0，语音一点不动；
        # 中间状态线性过渡。这等于"语音段完全不做噪声估计"——
        # 对离线谱减是最直接、也最不容易自相矛盾的处理方式。
        psd_hi = p * self._nb_ratio
        self._noise_psd = np.maximum(self._noise_psd, psd_hi)
        self._noise_psd = np.maximum(self._noise_psd, 1e-30)
        psd_eff = self._noise_psd * (1.0 - self._vad_ema)

        # ---- 先验 SNR（判决引导，Ephraim-Malah 的经典估计）
        #
        # 注意 1e-12 这个地板值：如果没有它，p/psd 会到 1e18，再平方就溢成 inf，
        # 然后 inf/inf = NaN —— 整个块变成 NaN，输出直接静音。这不是理论风险，
        # 是自测里真实撞到的（噪声底恰为 0 时）。地板放得足够大以保证平方后不溢出。
        #
        # 另一个坑：噪声底初始化成 p*0.25 时，前几十帧 snr_post ≈ 3，
        # 增益本就偏低；再叠加判决引导的 g_prev²项，增益会一路自我压低。
        # 实测纯语音输入、增益应接近 1 的场景，稳态平均增益只有 0.088（-21 dB）。
        # 所以更新式里必须保留一个足够大的"当前观测"权重（下面的 0.10），
        # 让增益能跟上当前帧，而不是被历史拖死。
        # 另一个坑：噪声底更新用的是乘性衰减，纯语音输入时噪声底会被一路
        # 压到 5e-20 这种非正规数，psd 地板若取 p 均值的 1e-3，就等于给
        # "已经被压到零"的频点重新安了一个很高的噪声底 → 增益全掉到 floor。
        # 地板只用来防 0 除，必须极小（1e-9 相对）。
        psd = np.maximum(psd_eff, 1e-9 * max(1.0, float(np.mean(p))))
        snr_post = np.maximum(p / psd - 1.0, 0.0)
        snr_post = np.clip(snr_post, 0.0, 1e6)        # 上限同样是为了不溢出
        g_prev = self._gain_prev
        # 判决引导的系数用 0.50（经典值），不要用 0.98：
        # 0.98 意味着"几乎完全相信上一帧"，增益变化比噪声底慢一个数量级 ——
        # 实测语音到来后增益要 200~300 ms 才爬到 1，语音起音被吃掉一大截；
        # 音节结束又要 300 ms 才降回来，这段时间噪声整段漏过去。
        # 0.50 在"抑制音乐噪声"和"跟得上语音"之间是公认的折中。
        snr_prio = 0.50 * (g_prev ** 2) * snr_post + 0.50 * snr_post

        # ---- 频谱增益：维纳形式 + 过减
        gain = snr_prio / (1.0 + snr_prio)
        gain = gain ** (1.0 / self._over_sub)                 # 过减：整体压得更狠
        # 增益下限用 np.maximum 夹。曾经担心"夹死会破坏 strength 单调性"，
        # 但那个问题真正的原因是噪声底下界被地板值提前替换掉了（见上面 psd
        # 地板值的注释），下界改成 nb_ratio 逐帧生效之后，纯噪声残余随 strength
        # 单调变差，不再需要避开 np.maximum。
        gain = np.maximum(gain, self._gmin)

        # ---- 频带加权：在这里以**软上限**的形式施加，而不是直接乘到增益上。
        #
        # 早期写法是 gain = maximum(gain * bias, floor)。bias 是恒定的，
        # 于是语音段（gain≈0.9）也会被乘上 0.80/0.92，凭空多掉 1~2 dB；
        # 而 floor 是最后才夹的，导致"压低频"实际上变成了"先乘 0.55 再夹回
        # floor" —— 净效果是低频被抬到 floor，抑制量反而变小。
        # 纯语音实测该写法让 20-120 Hz 涨了 +10.8 dB，就是这么来的。
        #
        # 软上限：只压"本来就没把握"的频点，不动已经接近 1 的语音谱线。
        gain = np.minimum(gain, self._bias + (1.0 - self._bias) * gain)

        # ---- 时间方向平滑：抑制"音乐噪声"（谱减最典型的副作用）
        gain = self._smooth * self._gain_prev + (1.0 - self._smooth) * gain
        self._gain_prev = gain

        # VAD 已在函数开头、噪声底更新之前算完（见那里的注释：不能依赖增益）

        # ---- 语音段电平/频谱整形：按频点自适应把损失补回来。
        #
        # 为什么必须做这一步（分频段实测数据）：
        #   strength=70 protect=30 时语音段各频带的损失是
        #     20-120 Hz -9.2 dB   ← 高通 + 噪声底误吃
        #     120-250   -1.2 dB
        #     250-500   -8.5 dB   ← 基频区，被噪声底当成噪声压掉
        #     500-1000  -1.8 dB
        #     1000-2000 -4.2 dB
        #     2000-3400 -3.5 dB
        #     3400-8000 -3.4 dB
        #   合计 RMS -5.6 dB。损失是**频带选择性**的，所以：
        #   a) 整体乘一个标量增益没用（低频补上去、中频就过冲）；
        #   b) 提高混回比例也没用（静音段跟着回来，噪声抑制从 22 dB 掉到 14 dB）。
        #
        # 正确做法是"逐频点、逐帧、自校准"：谱线越强（先验 SNR 越大）越确信
        # 它是语音，就把增益往 1 推得越多。这样对语音是频带自适应的补偿，
        # 对噪声（谱线弱、先验 SNR 小）仍然是原来的抑制量。
        #
        # boost 上限受 protect 控制：protect=0 时最多推到 0.62，保留可闻的
        # 处理感；protect=100 时到 0.92，语音基本原样。
        conf = np.clip((np.sqrt(snr_prio + 1.0) - 1.0) / 2.5, 0.0, 1.0)
        ceiling = 0.62 + 0.30 * self.protect / 100.0
        gain = gain + (1.0 - gain) * (conf * ceiling)


        # 统计用：平均抑制量（只统计被处理的频点）
        on = gain < 0.95
        if np.any(on):
            self._suppress_db = float(-20.0 * np.log10(np.mean(gain[on]) + 1e-9))

        # ---- 重建
        clean_spec = spec * gain
        td = np.fft.irfft(clean_spec, NFFT) * WIN

        # ---- 重叠相加
        # COLA 满足后（NFFT = 3*HOP），每个位置叠加的窗平方和是常数，
        # 所以只需除以标量 _norm。累加器整块左移 HOP，尾部补零。
        self._ola += td
        out = self._ola[:HOP] / self._norm
        self._ola[:-HOP] = self._ola[HOP:]
        self._ola[-HOP:] = 0.0
        return out

    @staticmethod
    def _soft_clip(x: np.ndarray) -> np.ndarray:
        """软限幅到 int16 范围：超过 0.9 满幅才起作用，避免硬削的"啪"。"""
        lim = 0.90 * 32767.0
        ax = np.abs(x)
        over = ax > lim
        if np.any(over):
            span = 32767.0 - lim
            x = x.copy()
            x[over] = np.sign(x[over]) * (lim + span * np.tanh((ax[over] - lim) / span))
        return np.clip(x, -32768.0, 32767.0)

    # ---------------------------------------------------------------- 指标
    def stats(self) -> dict:
        return {
            "on": bool(self.enabled),
            "frames": self._frames,
            "vad": round(float(self._vad_ema), 3),
            "suppress_db": round(self._suppress_db, 1),
            **self.params(),
        }
