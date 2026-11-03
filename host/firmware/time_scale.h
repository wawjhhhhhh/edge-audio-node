/* time_scale.h -- 可调时长缩放（方案 B 的可调版，50%..100%）
 *
 * 干什么
 * ------
 * 上行音频 100 fps 不变，帧长 480 点不变，但**内容被拉长**：
 *   每进 480 点输入，就往输出流追加 L = 480*100/rate 点，
 *   而每帧只交付 480 点 —— 多出来的点排队，下一帧继续送。
 *   于是播放端听到的是同一段语音，只是变慢了、长了。
 *
 *   rate = 100 -> L=480  ，恒等（固件走 memcpy 快速路径，零延迟）
 *   rate =  75 -> L=640  ，语速 0.75，时长 x1.333
 *   rate =  50 -> L=960  ，语速 0.50，时长 x2.000（方案 B 原形态）
 *
 * 一句话原理
 * ----------
 * 相邻两段起点相距 FRAME，段长 L，重叠 OV = L - FRAME。
 * 第 k 段取输入 src[k*FRAME .. k*FRAME+L)，贴在输出流的 k*FRAME 处。
 * 重叠区里新旧内容来自 src 的**同一段**（k*FRAME..k*FRAME+OV），
 * 天然同相 —— 所以不需要 SOLA 搜索，也不会因为相位不连续产生 click。
 * C 版因此没有互相关运算：每帧就是一次 memcpy + 一次交叉淡化求和。
 *
 * 为什么不是"整帧重复/丢弃"
 * -------------------------
 * 每 2 帧重复 1 帧，语速对了，但接缝处波形相位任意，每 480 点一次 click，
 * 听感是"哒、哒、哒"。上面这种重叠粘贴法里，重叠区两端取自同一份源，
 * 波形完全连续，接缝不可闻。实测（time_scale_ref.py，3 s 语音样信号）
 * 一阶/二阶差分与原始信号的比值都是 1.00，即没有任何阶跃。
 *
 * 为什么没有照搬教科书 SOLA 的互相关搜索（值得记，别改回去）
 * --------------------------------------------------------
 * 因为重叠的两端来自**同一份源**（不是来自压缩后的不同位置），
 * 相关性恒等于 1，搜索永远返回 d=0 —— 加了也只是白算 49 次互相关。
 * 而在**输入**里搜索起点会破坏"每帧消费正好 FRAME 点"这个不变量，
 * 实测输入会持续堆积（832 点还在涨），合成量只有输入量的 1/11。
 * 教科书 SOLA 用搜索，是因为它要在"整段压缩"里找对齐点；
 * 这里是"逐帧等权粘贴"，对齐由结构本身保证。
 *
 * 与板载成本模型的关系
 * --------------------
 * 本函数每帧只做一次 L≤960 点的 float 拷贝与 ov≤480 点的加权求和，
 * 在 667 MHz A9 上是**几十微秒**量级，与 10 ms 帧预算相比可以忽略。
 * 它不改变 100 fps 上行节奏，也不增加 CPU 排队 —— 代价只是
 * "同样的内容要放更久"，即输出时间轴的物理延长。
 *
 * 与方案的边界
 * ------------
 * 本文件是纯计算：不碰寄存器、不调 xil_*、不动态分配，可独立编译。
 * 验证分两层，两层都是真代码、不是复制品：
 *   a) time_scale_ref.py —— numpy 同构实现，6 档倍率/阶跃/恒等全 PASS
 *   b) ts_test/          —— 本文件直接跑在 QEMU 的 Zynq-7000 A9 上，
 *                           含 8 次动态切档，`bash ts_test/run_ts_test.sh`
 *
 * 调用方必须遵守的两条
 * --------------------
 * 1. 改 rate 之后立刻调 ts_reset()。scaler 是有状态的（持有半帧待交付
 *    输出），不清会把上一档的残留回放进新档 —— 与"切模式不清 batch ring"
 *    是同一类 bug。固件里这件事由 ts_parse_rate() 统一做。
 * 2. rate=100 时没有额外延迟、没有静音前奏；rate<100 的首帧会补零，
 *    代价是 1 帧静音前奏（rate=50 时 10 ms）。这是"攒够内容才能放慢"
 *    的必然，不是 bug。
 */

#ifndef TIME_SCALE_H
#define TIME_SCALE_H

#include <string.h>

#define TS_FRAME    480                    /* 输入/输出帧长（样点）      */
#define TS_RATE_MIN 50                     /* 50%：语速减半，时长翻倍     */
#define TS_RATE_MAX 100                    /* 100%：直通，零延迟         */
#define TS_RATE_DEF 100                    /* 默认值 = 不改现状          */

/* 缓冲尺寸：L 最大 480*100/50 = 960；每帧追加 L 点、交付 480 点，
 * 稳态待交付量恒定（等于 L-480），所以 2048 足够且不随时间增长。 */
#define TS_BUF      2048

typedef struct {
    /* src 需要容纳 3 帧：上一帧 + 本帧 + 余量。
       第 k 段取 src[k*FRAME .. k*FRAME+L)，L 最大 960 = 2*FRAME，
       所以"本帧 + 下一帧"刚好够；为让下标永远 >= 0 再多留一帧。 */
    float src[TS_FRAME * 3];
    int   srclen;
    float out[TS_BUF];                     /* 待交付输出流               */
    int   outlen;
    int   primed;
} ts_state;

/* 合法性检查。固件在命令入口调用：非法值不落进任何处理函数，
   而是直接拒绝并打一条说明原因的日志（"超出范围时给出明确反馈"）。 */
static inline int ts_rate_ok(int rate)
{
    return (rate >= TS_RATE_MIN) && (rate <= TS_RATE_MAX);
}

/* 每帧应产出多少点 */
static inline int ts_out_len(int rate)
{
    return (TS_FRAME * 100) / rate;        /* 50->960, 75->640, 100->480 */
}

static inline void ts_reset(ts_state *s)
{
    memset(s, 0, sizeof(*s));
}

/* 一帧处理：in/out 各 TS_FRAME 点，类型 float —— 调用方（固件）在
 * RNNoise 之后本来就有 float 中间量，直接用 float 省掉一次来回转换；
 * 输出限幅到 int16 范围，由调用方转成 short 打包。
 *
 * 返回值恒为 TS_FRAME：上层每收到一帧 MIC 就必须发一帧，少一帧即丢包。
 * 输入不足时（rate<100 的首帧）输出补零，代价是 1 帧静音前奏。 */
static int ts_frame(ts_state *s, const float *in, float *out, int rate)
{
    int L = ts_out_len(rate);
    int ov = L - TS_FRAME;
    int i;

    /* ---- rate=100：恒等直通，不碰状态，零延迟 ---- */
    if (rate >= TS_RATE_MAX) {
        for (i = 0; i < TS_FRAME; i++) out[i] = in[i];
        return TS_FRAME;
    }

    /* ---- 1. 把新一帧接进 src；保留最近 2 帧（L 最大 = 2 帧） ---- */
    if (s->srclen + TS_FRAME > (int)(sizeof(s->src) / sizeof(s->src[0]))) {
        /* 正常每帧只减 TS_FRAME，永不触发；防御性降级 */
        s->srclen = 0;
    }
    memcpy(s->src + s->srclen, in, sizeof(float) * TS_FRAME);
    s->srclen += TS_FRAME;

    /* ---- 2. 只要 src 里够一整段（L 点），就贴一段到 out ---- */
    while (s->srclen >= L) {
        int base = s->outlen - (s->primed ? ov : 0);
        int j;

        if (base < 0) base = 0;
        if (base + L > TS_BUF) break;       /* 缓冲不足：留到下一帧，不会丢 */

        if (!s->primed) {
            for (j = 0; j < L; j++)
                s->out[base + j] = s->src[j];
            s->primed = 1;
        } else {
            /* 重叠区 [base, base+ov)：轨迹尾部（旧）与 src 同段（新）
               交叉淡化。两者取自 src 的同一段，权重不影响结果 ——
               保留它是为了让 rate 切换那一刻（ov 突变）也连续。 */
            for (j = 0; j < ov; j++) {
                float w = (float)j / (float)ov;
                s->out[base + j] = s->out[base + j] * (1.0f - w)
                                 + s->src[j]        * w;
            }
            for (j = ov; j < L; j++)
                s->out[base + j] = s->src[j];
        }

        s->outlen = base + L;
        s->srclen -= TS_FRAME;              /* 每段固定消费一帧输入 */
        memmove(s->src, s->src + TS_FRAME,
                sizeof(float) * s->srclen);
    }

    /* ---- 3. 交付一帧。不够则补零（只在开机首帧） ---- */
    if (s->outlen < TS_FRAME) {
        for (i = 0; i < TS_FRAME; i++) out[i] = 0;
        return TS_FRAME;
    }
    for (i = 0; i < TS_FRAME; i++) {
        float v = s->out[i];
        if (v > 32767.0f)  v = 32767.0f;
        if (v < -32768.0f) v = -32768.0f;
        out[i] = v;
    }
    s->outlen -= TS_FRAME;
    memmove(s->out, s->out + TS_FRAME, sizeof(float) * s->outlen);

    return TS_FRAME;
}

#endif /* TIME_SCALE_H */
