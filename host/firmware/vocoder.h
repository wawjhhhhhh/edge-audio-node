/* vocoder.h -- LPC 参数语音编解码（分析/合成），48 kHz 单声道
 *
 * 为什么要有它
 * ------------
 * RNNoise 的极限是"把噪声压到听不见的底噪"；参数化声码器是另一条路：
 * 每帧只保留说话声的骨架——频谱包络（LPC/反射系数）、基频、增益、清浊音——
 * 再用这几个参数驱动声道模型把声音**重新造出来**。噪声没有稳定的基频和
 * 共振峰，进不了这几十个参数，因此原理上被丢掉。
 *
 * 代价（必须写清楚，别自我感动）：
 *   1. 音色能保住（包络传了），但自然度下降 —— 7 kbps 级别是"机器人腔"；
 *   2. s / sh / f 这类清辅音靠"清音帧 + 白噪声"近似，容易发闷；
 *   3. 强噪声下清浊判决会把噪声判成清音，形成"水声"。
 *
 * 结构：20 ms 一帧（48 kHz = 960 点），16 阶 LPC，基频 60–400 Hz。
 * 参数位宽：反射系数 16×8 bit + 基频 7 bit + 增益 8 bit + 清浊 1 bit
 *           = 144 bit / 20 ms = 7.2 kbps（够"再生"演示；LPC-10 是 2.4 kbps）
 *
 * 量化用反射系数 k（|k|<1 天然保证合成滤波器稳定），不做 LAR/对数量化，
 * 避免在裸机上引入 exp/log。
 *
 * 不依赖 Xilinx 头文件，可在 QEMU 裸机与 PC 上跑。
 */
#ifndef VOCODER_H
#define VOCODER_H

#define VOC_RATE      48000
#define VOC_FRAME     960          /* 20 ms */
#define VOC_ORDER     16
#define VOC_DEC       6            /* 基频搜索抽取比：48k/6 = 8k */
#define VOC_LAG_MIN   20           /* 8 kHz 下 20 点 = 400 Hz */
#define VOC_LAG_MAX   133          /* 8 kHz 下 133 点 = 60 Hz */
#define VOC_GAIN_SCALE 255.0f      /* 增益量化标度（rms 1.0 → 255，8 bit 满量程） */

/* 调试用：最近一帧合成器的原始 RMS / 归一化系数 / 目标 RMS（自测里可打印） */
static float voc_dbg_rms, voc_dbg_scale, voc_dbg_target;

typedef struct {
    unsigned char k[VOC_ORDER];    /* 量化反射系数：0..255 映射 -1..+1 */
    unsigned char gain;            /* 量化残差 rms */
    unsigned char pitch;           /* 1..128 量化基周期；0 = 清音 */
} VocParams;

typedef struct {
    float x_prev;                  /* 预加重历史 */
    float hp_hist[VOC_ORDER];      /* 预加重后的历史（求 LPC 残差做基频用） */    float syn[VOC_ORDER];          /* 合成滤波器状态 */
    float de_prev;                 /* 去加重历史 */
    float phase;                   /* 浊音脉冲相位 */
    unsigned rnd;                  /* 噪声发生器 */
    int started;
} Vocoder;

/* ---- 小工具：不依赖 libm ------------------------------------------- */

static float voc_absf(float x) { return x < 0.0f ? -x : x; }

static float voc_cos(float x)
{
    const float PI = 3.14159265358979f;
    const float TWO_PI = 6.28318530717959f;
    float x2, term, sum;
    int k;
    while (x > PI) x -= TWO_PI;
    while (x < -PI) x += TWO_PI;
    x2 = x * x;
    term = 1.0f;
    sum = 1.0f;
    for (k = 1; k <= 8; k++) {
        term *= -x2 / (float)((2 * k - 1) * (2 * k));
        sum += term;
    }
    return sum;
}

static float voc_sqrt(float x)
{
    float y;
    int i;
    if (x <= 0.0f) return 0.0f;
    y = (x > 1.0f) ? x * 0.5f : 1.0f;
    for (i = 0; i < 24; i++) y = 0.5f * (y + x / y);
    return y;
}

static unsigned voc_rand(Vocoder *v)
{
    unsigned x = v->rnd;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    v->rnd = x;
    return x;
}

/* ---- 反射系数 <-> LPC 系数 ----------------------------------------- */

/* 阶降：a[0..m] -> k[1..m]（a[0] 必须为 1） */
static void voc_a_to_k(const float *a_in, float *k)
{
    float a[VOC_ORDER + 1];
    int i, m;
    for (i = 0; i <= VOC_ORDER; i++) a[i] = a_in[i];
    for (m = VOC_ORDER; m >= 1; m--) {
        float km = a[m];
        float d = 1.0f - km * km;
        if (voc_absf(km) > 0.999f) km = (km > 0.0f) ? 0.999f : -0.999f;
        k[m] = km;
        if (d < 1e-5f) d = 1e-5f;
        for (i = 1; i < m; i++) {
            a[i] = (a[i] - km * a[m - i]) / d;
        }
    }
}

/* 阶升：k[1..m] -> a[0..m] */
static void voc_k_to_a(const float *k, float *a)
{
    float tmp[VOC_ORDER + 1];
    int i, m;
    for (i = 0; i <= VOC_ORDER; i++) { a[i] = 0.0f; tmp[i] = 0.0f; }
    a[0] = 1.0f;
    for (m = 1; m <= VOC_ORDER; m++) {
        for (i = 1; i < m; i++) tmp[i] = a[i] + k[m] * a[m - i];
        for (i = 1; i < m; i++) a[i] = tmp[i];
        a[m] = k[m];
    }
}

/* ---- Levinson-Durbin ------------------------------------------------- */

static int voc_levinson(const float *r, float *a, float *err)
{
    float tmp[VOC_ORDER + 1];
    float e = r[0];
    int i, m;
    if (!(e > 0.0f)) return 0;
    for (i = 0; i <= VOC_ORDER; i++) { a[i] = 0.0f; tmp[i] = 0.0f; }
    a[0] = 1.0f;
    for (m = 1; m <= VOC_ORDER; m++) {
        float acc = r[m];
        float k;
        for (i = 1; i < m; i++) acc += a[i] * r[m - i];
        k = -acc / e;
        if (k > 0.995f) k = 0.995f;
        if (k < -0.995f) k = -0.995f;
        for (i = 1; i < m; i++) tmp[i] = a[i] + k * a[m - i];
        for (i = 1; i < m; i++) a[i] = tmp[i];
        a[m] = k;
        e *= (1.0f - k * k);
        if (e < 1e-10f) { e = 1e-10f; break; }
    }
    *err = e;
    return 1;
}

/* ---- 分析一帧 -------------------------------------------------------- */

static void voc_analyze(Vocoder *v, const short *x, VocParams *p)
{
    static float w[VOC_FRAME];
    static float hp_only[VOC_FRAME];
    static float d[VOC_FRAME / VOC_DEC];
    float r[VOC_ORDER + 1];
    float a[VOC_ORDER + 1];
    float kf[VOC_ORDER + 1];
    float err = 0.0f, rms;
    int i, j, lag;
    int nd = VOC_FRAME / VOC_DEC;
    int best_lag = 0;
    float best = 0.0f;

    /* 预加重 + Hamming 窗 */
    for (i = 0; i < VOC_FRAME; i++) {
        float cur = (float)x[i] / 32768.0f;
        float hp = cur - 0.9375f * v->x_prev;
        float win = 0.54f - 0.46f * voc_cos(6.28318530717959f * (float)i / (float)(VOC_FRAME - 1));
        v->x_prev = cur;
        hp_only[i] = hp;
        w[i] = hp * win;
    }

    /* 自相关 */
    for (lag = 0; lag <= VOC_ORDER; lag++) {
        float s = 0.0f;
        for (i = lag; i < VOC_FRAME; i++) s += w[i] * w[i - lag];
        r[lag] = s;
    }
    r[0] *= 1.0001f;
    if (!voc_levinson(r, a, &err)) {
        for (i = 1; i <= VOC_ORDER; i++) a[i] = 0.0f;
        err = (r[0] > 0.0f) ? r[0] : 1e-10f;
    }

    /* 基频：先对"预加重信号"做 LPC 逆滤波得到残差，再 6:1 抽取做归一化自相关。
     * 直接用原始信号做自相关会被共振峰的周期性拖出倍频/半频错判，
     * 残差里只剩声门脉冲，周期才干净。 */
    {
        static float res[VOC_FRAME];
        float sc[VOC_LAG_MAX + 1];
        int k;
        /* 逆滤波（带上一帧的 16 个历史） */
        for (i = 0; i < VOC_FRAME; i++) {
            float s = hp_only[i];
            for (k = 1; k <= VOC_ORDER; k++) {
                int idx = i - k;
                s -= a[k] * (idx >= 0 ? hp_only[idx] : v->hp_hist[VOC_ORDER + idx]);
            }
            res[i] = s;
        }
        for (i = 0; i < nd; i++) {
            float s = 0.0f;
            for (j = 0; j < VOC_DEC; j++) s += res[i * VOC_DEC + j];
            d[i] = s;
        }
        for (lag = 0; lag <= VOC_LAG_MAX; lag++) sc[lag] = -1.0f;
        {
            int lo = VOC_LAG_MIN, hi = VOC_LAG_MAX;
            if (hi > nd - 2) hi = nd - 2;
            for (lag = lo; lag <= hi; lag++) {
                float s = 0.0f, e1 = 0.0f, e2 = 0.0f;
                for (i = lag; i < nd; i++) {
                    s += d[i] * d[i - lag];
                    e1 += d[i] * d[i];
                    e2 += d[i - lag] * d[i - lag];
                }
                if (e1 > 1e-9f && e2 > 1e-9f) {
                    sc[lag] = s / (0.5f * (e1 + e2));
                    if (sc[lag] > best) { best = sc[lag]; best_lag = lag; }
                }
            }
        }
        /* 倍频纠偏：取"第一个足够强的峰"，避免 2 倍周期胜出导致音高掉八度 */
        if (best_lag > 0) {
            int l2;
            for (l2 = VOC_LAG_MIN; l2 < best_lag; l2++) {
                if (sc[l2] > 0.0f && sc[l2] >= 0.90f * best) {
                    if (sc[l2] / (float)l2 > sc[best_lag] / (float)best_lag) {
                        best_lag = l2;
                        best = sc[l2];
                    }
                    break;
                }
            }
        }
    }
    /* 更新逆滤波历史 */
    for (i = 0; i < VOC_ORDER; i++) v->hp_hist[i] = hp_only[VOC_FRAME - VOC_ORDER + i];
    /* 量化：反射系数 8 bit，基周期 7 bit，增益 8 bit */
    voc_a_to_k(a, kf);
    for (i = 1; i <= VOC_ORDER; i++) {
        float q = (kf[i] + 1.0f) * 127.5f;
        int qq = (int)(q + 0.5f);
        if (qq < 0) qq = 0;
        if (qq > 255) qq = 255;
        p->k[i - 1] = (unsigned char)qq;
    }

    p->pitch = 0;
    if (best > 0.45f && best_lag > 0) {
        int lag48 = best_lag * VOC_DEC;                 /* 120..798 */
        int q = (lag48 - 120) * 255 / 680;              /* 0..255，步进 2.7 点 ≈ 0.7% */
        if (q < 0) q = 0;
        if (q > 255) q = 255;
        p->pitch = (unsigned char)(q + 1);              /* 0 保留给清音 */
    }

    rms = 0.0f;
    for (i = 0; i < VOC_FRAME; i++) {
        float cur = (float)x[i] / 32768.0f;
        rms += cur * cur;
    }
    rms = voc_sqrt(rms / (float)VOC_FRAME);   /* 送的是"原信号"的 RMS，不是残差 */
    {
        int q = (int)(rms * VOC_GAIN_SCALE + 0.5f);
        if (q < 0) q = 0;
        if (q > 255) q = 255;
        p->gain = (unsigned char)q;
    }
    v->started = 1;
}

/* ---- 合成一帧 -------------------------------------------------------- */

static void voc_synthesize(Vocoder *v, const VocParams *p, short *out)
{
    static float y[VOC_FRAME];
    float kf[VOC_ORDER + 1];
    float a[VOC_ORDER + 1];
    float target, amp, lag = 0.0f, rms = 0.0f, scale;
    int i, m;

    kf[0] = 1.0f;
    for (i = 1; i <= VOC_ORDER; i++) {
        kf[i] = ((float)p->k[i - 1] / 127.5f) - 1.0f;
    }
    voc_k_to_a(kf, a);

    target = (float)p->gain / VOC_GAIN_SCALE;   /* 目标 RMS */
    if (p->pitch > 0) {
        lag = 120.0f + ((float)(p->pitch - 1) / 255.0f) * 680.0f;
        amp = 1.0f;                             /* 脉冲高度先给 1，最后统一归一化 */
    } else {
        amp = 0.5f;
    }

    for (i = 0; i < VOC_FRAME; i++) {
        float exc, yy;
        if (lag > 0.0f) {
            v->phase += 1.0f;
            if (v->phase >= lag) v->phase -= lag;
            exc = (v->phase < 1.0f) ? amp : 0.0f;
        } else {
            unsigned rn = voc_rand(v);
            float u = ((float)(int)(rn >> 8) / 8388608.0f) - 1.0f;   /* -1..1 */
            exc = u * amp;
        }
        yy = exc;
        for (m = 1; m <= VOC_ORDER; m++) yy -= a[m] * v->syn[m - 1];

        /* 数值防爆：16 阶直接型在高 Q（强周期信号）下会共振到很大，
         * 一旦状态超过 40 就把整组状态等比缩回，形状不变、数值不失控。 */
        if (yy > 40.0f || yy < -40.0f) {
            float g = 40.0f / (yy < 0.0f ? -yy : yy);
            for (m = 0; m < VOC_ORDER; m++) v->syn[m] *= g;
            yy *= g;
        }
        for (m = VOC_ORDER - 1; m > 0; m--) v->syn[m] = v->syn[m - 1];
        v->syn[0] = yy;

        yy += 0.9375f * v->de_prev;
        v->de_prev = yy;
        y[i] = yy;
    }

    /* 帧级电平归一化：把合成结果拉到原帧 RMS（等于把"增益"这个参数用起来），
     * 再用软限幅代替硬削波，避免整帧响度对了却在峰值上削出方波。 */
    for (i = 0; i < VOC_FRAME; i++) rms += y[i] * y[i];
    rms = voc_sqrt(rms / (float)VOC_FRAME);
    scale = (rms > 1e-6f) ? (target / rms) : 0.0f;
    if (scale > 1e4f) scale = 1e4f;
    voc_dbg_rms = rms;
    voc_dbg_scale = scale;
    voc_dbg_target = target;

    for (i = 0; i < VOC_FRAME; i++) {
        float s = y[i] * scale;
        float a4 = s < 0.0f ? -s : s;
        if (a4 > 0.85f) {                    /* 0.85 以上做平滑压缩 */
            float over = a4 - 0.85f;
            float head = 0.15f;              /* 0.85..1.0 的余量 */
            a4 = 0.85f + head * (over / (over + head));
            s = (s < 0.0f) ? -a4 : a4;
        }
        out[i] = (short)(s * 32767.0f);
    }
}

#endif /* VOCODER_H */
