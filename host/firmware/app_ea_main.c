/* udp_link.c -- bidirectional audio over 100 Mbps Ethernet (eth_audio design)
 *
 * MIC  path: ES8388 ADC -> I2S -> audio_axis_core -> AXIS -> DMA1 S2MM -> DDR
 *            -> 2 UDP packets per 10 ms frame -> PC (port 50505)
 *
 * DAC  path: PC (port 50506) -> UDP -> 100 ms playback buffers -> DMA1 MM2S
 *            -> AXIS -> audio_axis_core s_axis -> I2S -> ES8388 DAC -> headphones
 *
 * The DMA1 MM2S for the DAC blocks on the core's 16-beat TX FIFO, so a large
 * transfer paces itself at exactly the I2S rate; we only have to keep whole
 * 100 ms buffers queued ahead of the play pointer.
 *
 * Startup plays a 440 Hz tone out of the DAC so the audio codec path can be
 * verified with no network and no PC; the first UDP packet switches it to the
 * network stream.
 *
 * UDP payload format (both directions):
 *   8 B header: 'A','U', seq_hi,seq_lo, chunk_hi,chunk_lo, total_hi,total_lo
 *   then PCM: 48 kHz / 16 bit / stereo, little endian, [15:0]=left [31:16]=right
 */
#include <stdio.h>
#include <string.h>
#include "xil_io.h"
#include "xil_printf.h"
#include "xil_cache.h"
#include "xparameters.h"
#include "sleep.h"
#include "rnnoise.h"
#include <math.h>
#include "time_scale.h"

#define DMA_ETH_BASE   0x40400000u
#define DMA_AUD_BASE   0x40410000u

#define DMA_TX   0x00u
#define DMA_RX   0x30u
#define R_CR     0x00u
#define R_SR     0x04u
#define R_ADDR   0x18u
#define R_LEN    0x28u

#define CR_RESET   0x00000004u
#define CR_RUNSTOP 0x00000001u
#define SR_IOC     0x00001000u
#define SR_ERR     0x00000770u

#define ETH_BASE   0x41000000u
#define RES_BASE   0x08000000u
#define GPIO_BASE  0x41200000u
/* 2026-11-02 包6：AXI GPIO 第二通道（BD 里 C_IS_DUAL=1 / GPIO2_WIDTH=2），
   低两位给 PL 里的 dac_split 选双 DAC 输出模式：
     0 = M0 两口同源（默认）
     1 = M1 板载只放左、外接只放右
     2 = M2 板载静音、外接放完整立体声（配合 PC 推单声道素材） */
/* 同一个 AXI GPIO 双通道实例的通道2：数据寄存器在 0x41200008
   （通道2 数据寄存器 = 0x41200000+0x8，TRI 在 +0x4），不是独立的
   64KB 外设。旧值 0x41210000 是未映射地址，写进去 GPIO 纹丝不动。 */
#define GPIO_MODE_BASE 0x41200008u

#define MAC0   0x02u
#define MAC5   0x01u
#define BOARD_IP0 169u
#define BOARD_IP1 254u
#define BOARD_IP2 191u
#define BOARD_IP3 50u

#define PORT_MIC 50505u
#define PORT_IN  50506u
#define PORT_CTL 50507u  /* one-byte control: '0' bypass, '1' RNNoise,
                            '2' gate, 'a'..'d' noise level, 't' tone */
#define PORT_STAT 50508u /* 12-byte 'AS' status broadcast every 250 ms:
                            the board's OWN view of g_dsp_mode & friends, so a
                            PC can show what the board is really doing instead
                            of guessing from what it last sent. */

#ifndef MIC_GAIN
#define MIC_GAIN 8
#endif

#define FRAME_WORDS   480u         /* 10 ms @ 48 kHz stereo          */
#define AUDIO_BYTES   1920u
#define CHUNK_WORDS   240u         /* half a frame                   */
#define CHUNK_BYTES   960u
#define HDR_BYTES     8u
#define UDP_PAYLOAD   (HDR_BYTES + CHUNK_BYTES)

#define PB_N       4u              /* playback buffers, 100 ms each  */
#define PB_WORDS   4800u
#define RXP_N      4u              /* ethernet receive buffers       */
#define RXP_BYTES  1600u

static u8  txbuf[1514]   __attribute__((aligned(64)));
static u8  pl[UDP_PAYLOAD] __attribute__((aligned(64)));

static u32 micbuf[2][FRAME_WORDS] __attribute__((aligned(64)));
static u8  rxpkt[RXP_N][RXP_BYTES] __attribute__((aligned(64)));
static u32 pbuf[PB_N][PB_WORDS]   __attribute__((aligned(64)));

static u32 pb_fill[PB_N];
static u8  pb_ready[PB_N];
static int g_gain = 32768;        /* Q15 output gain, 32768 == 0 dB (-0)  */
static int pb_wr;                 /* buffer the network fills */
static u32 g_arm_cr, g_arm_sr, g_arm_sr2;

static const s16 sine64[64] = {
       0,   785,  1567,  2340,  3096,  3827,  4526,  5186,
    5798,  6357,  6857,  7292,  7658,  7951,  8169,  8309,
    8371,  8355,  8262,  8094,  7853,  7543,  7167,  6730,
    6237,  5694,  5107,  4483,  3829,  3152,  2459,  1759,
    1059,   367,  -318,  -987, -1631, -2242, -2812, -3333,
   -3798, -4202, -4540, -4808, -5003, -5124, -5169, -5139,
   -5035, -4859, -4614, -4303, -3930, -3501, -3022, -2500,
   -1944, -1362,  -764,  -159,   446,  1041,  1618,  2169
};

/* ---------------- rate instrumentation (read-only) ----------------
   Free-running ARM global timer low word at 0xF8F00200.  MEASURED on this
   board: g_frame_dt (one 480-word MIC frame) reads 0x54CBE = 347326, and the
   PC receives exactly 100 frames/s, so the timer is ~34.7 MHz, NOT 333 MHz --
   one tick is ~28.8 ns, one MIC frame (10 ms) is ~347k ticks, one 25 ms DAC
   chunk is ~868k ticks.  Differences over <10 s cannot wrap the 32-bit low
   half, so a plain subtraction is safe.  Reported in hex because xil_printf
   has no %lu. */
static u32 g_t_arm;      /* timer snapshot taken right after an MM2S arm     */
static u32 g_dac_dt;     /* first measured MM2S transfer duration, AXI ticks */
static u32 g_frame_dt;   /* first measured MIC frame interval, AXI ticks     */
static u32 g_last_t;     /* timer at the previous MIC frame                  */
static u32 g_len_rb;     /* R_LEN read back right after the LEN write        */
static u32 g_sil_t;      /* timer when the DAC ran out of data (0 = has data) */
#define TIMER_LOW 0xF8F00200u
/* Fallback to the self-test tone after "30 ms" of silence.  MEASURED: the ARM
   global timer on this board counts ~34.7 MHz (see g_frame_dt above), so 1e7
   ticks is really ~288 ms, not 30 ms.  Left at 288 ms on purpose for now: it
   must not fire in the middle of a boundary stall, otherwise the fallback
   itself would mask the very dropout being measured. */
#define STARVE_TICKS 10000000u

/* ---------------- v8 diagnostics: everything goes into res[] in DDR -------
 * Why not xil_printf: the old 330-character status line costs ~28 ms on the
 * 115200 UART, and the PL TX FIFO only holds 10.7 ms, so printing during
 * streaming CREATES the very dropout we are hunting.  Nothing is printed
 * while the stream runs; xsdb dumps res[] afterwards (flash_eth_audio.tcl).
 *
 * res[] layout (all u32, DDR 0x08000000):
 *   [0]  'LNK1'          [1]  n_mic            [2]  n_dac
 *   [3]  n_under         [4]  n_pkt            [5]  n_mic_err
 *   [6]  rms             [7]  drst | rxrst<<8 | rxerr<<16 | clobber<<24
 *   [8]  boundary ring write index
 *   [9]  bnd_n   [10] bnd_sum   [11] bnd_max   [12] bnd_gt5ms
 *   [13] switch | lead2<<8 | tone_in<<16 | tone_out<<24
 *   [14] chunk ring write index
 *   [15] g_frame_dt  [16] g_dac_dt
 *   [17] chk_n  [18] chk_sum  [19] chk_min  [20] chk_max
 *   [21] pgap8 | pgap15<<8 | txstall<<16
 *   [22] pgap_max  [23] n_rx_any (every frame the MAC delivered)
 *   [24..55]  32 x boundary record: (ticks & 0xFFFFFF) | stream<<24 | gt<<25
 *   [56] buf_min  [57] buf_n      -- shortest whole-buffer period, in ticks
 *   [58] TIMER_LOW   [59] TIMER_HIGH at report time (absolute tick snapshot:
 *        the delta between two reports is exactly 5 s, so this CALIBRATES the
 *        timer; every other measurement is only readable once we know it)
 *   [60] frame interval 200->201 (steady state, not the startup artifact)
 *   [61] bnd window max (this 5 s window only)
 *   [62] last boundary: raw now2   [63] last boundary: g_t_ioc
 *   [30] dly_frames  [31] dly_fill
 *   [32] rn_target (ladder)  [33] rn_in_fill  [34] rn_out_fill
 *   [35] rn_qmax   [36] rn_qdrops   [37] rn_batches
 *   [38] lat_off  [39] dsp_mode          -- so a dump after a test can prove
 *        which configuration was actually live, not which one was requested
 *   [64..87]  spare
 *   [88..127] 40 x chunk record:    (ticks & 0xFFFFFF) | stream<<24
 * Boundary record = unarmed interval at a 100 ms buffer boundary, i.e. exactly
 * how long the DAC was left without a queued chunk (the PL FIFO covers 10.7 ms
 * of it).  Chunk record = MM2S transfer duration; a healthy chunk takes
 * (1200 - fifo_occupancy)/48k, so a SHORT chunk proves the FIFO was empty.
 */
#define BND_RING 32
#define CHK_RING 40
static u32 g_t_ioc;      /* timer when the last DAC chunk completed */
static u32 g_t_ioc_v;    /* g_t_ioc is valid */
static u32 g_bnd_sum, g_bnd_max, g_bnd_n, g_bnd_gt, g_bnd_win_max;
static u32 g_chk_sum, g_chk_max, g_chk_min, g_chk_n;
static u32 g_bri, g_cri;
static u32 g_app_over;   /* packets appended while pb_wr == pb_rd (clobber!) */
static u32 g_switch, g_lead2, g_tone_in, g_tone_out, g_txstall;
static u32 g_pgap8, g_pgap15, g_pgap_max, g_pgap_sum, g_pkt_frame;
static u32 g_t_pkt, g_t_pkt_v;
static u32 g_frame_dt2, g_fd_ref;   /* steady-state MIC frame interval      */
static u32 g_t_pbnd, g_buf_min, g_buf_n, g_buf_v;  /* whole-buffer period    */
static u32 g_pkt_seen;               /* n_pkt at the previous report         */

/* ---------------- AXI DMA helpers ---------------- */

/* Start a transfer WITHOUT the soft reset.
 *
 * MEASURED ON THIS BOARD (PRB8): writing CR.RESET to *either* channel's register
 * block resets the WHOLE axi_dma IP, not just that channel.  A soft reset on the
 * S2MM side therefore leaves the MM2S channel in its reset state
 * (DMASR=0x00000001 Halted-only, DMACR.RS=0), and the DAC transfer that was
 * running silently disappears: no IOC, no error, dac count frozen.
 *
 * The S2MM (mic) channel is re-armed every 10 ms, so it survives such a reset;
 * the MM2S (DAC) channel is armed once per 100 ms buffer, so it does not.
 * Hence: reset once at init, then only ever use the reset-free start. */
static void dma_start_nr(u32 base, u32 off, u32 addr, u32 len)
{
    u32 sr = Xil_In32(base + off + R_SR);

    Xil_Out32(base + off + R_SR, sr | SR_IOC | SR_ERR);      /* clear sticky */
    Xil_Out32(base + off + R_CR, CR_RUNSTOP);
    Xil_Out32(base + off + R_ADDR, addr);
    Xil_Out32(base + off + R_LEN, len);
}

/* NOTE: there is deliberately no "reset + start" helper any more.  Resetting an
 * AXI DMA resets BOTH of its channels, so any repeated reset kills whichever
 * channel is already running (that is what kept the DAC and the Ethernet RX
 * dead).  Every DMA is now reset exactly once at startup, by hand, and all
 * arming goes through dma_start_nr(). */

/* 1 = halted in reset state and not running (i.e. somebody reset the IP). */
static int dma_is_reset(u32 base, u32 off)
{
    return (Xil_In32(base + off + R_SR) == 0x00000001u) &&
           ((Xil_In32(base + off + R_CR) & CR_RUNSTOP) == 0u);
}

/* 1 = complete, -1 = error, 0 = still running.  Never sleeps: a 1 ms sleep
   here used to cost a full millisecond of audio per frame. */
static int dma_poll(u32 base, u32 off)
{
    u32 sr = Xil_In32(base + off + R_SR);

    if (sr & SR_ERR) { Xil_Out32(base + off + R_SR, sr); return -1; }
    if (sr & SR_IOC) { Xil_Out32(base + off + R_SR, sr | SR_IOC); return 1; }
    return 0;
}

static int dma_wait(u32 base, u32 off, u32 spins)
{
    int r;

    while (spins--) {
        r = dma_poll(base, off);
        if (r != 0) return r;
    }
    return 0;
}

/* ---------------- UDP framing ---------------- */
static u32 ip_csum(const u8 *p, int n)
{
    u32 s = 0;
    int i;

    for (i = 0; i + 1 < n; i += 2) s += ((u32)p[i] << 8) | p[i + 1];
    if (n & 1) s += (u32)p[i] << 8;
    while (s >> 16) s = (s & 0xFFFFu) + (s >> 16);
    return (~s) & 0xFFFFu;
}

/* Build + transmit one UDP broadcast frame.
 *
 * BUG FIXED HERE (measured from the PC side: every packet carried 952 PCM
 * bytes instead of 964+...).  The 8-byte "AU" header lives at udp[8..15] and
 * the PCM starts at udp[16], but the length fields only accounted for the UDP
 * header plus one 8-byte header, so the transmitted frame was 8 bytes short
 * and the LAST 8 BYTES OF EVERY PACKET (2 stereo samples) were cut off.
 * UDP length = udp hdr(8) + AU hdr(8) + n; IP total = 20 + that. */
static void send_frame(const u8 *payload, u32 n, char t0, char t1,
                       u32 seq, u32 chunk, u32 lenfield, u32 port)
{
    static const u8 bcast[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };
    static int tx_busy = 0;
    u8 *e = txbuf, *ip, *udp;
    u32 total = 20u + 8u + 8u + n, c, flen;

    /* Reap the PREVIOUS frame before touching the shared txbuf: normally the
       90 us frame started 5 ms ago is long done, so this is one register read.
       The old code blocked on the frame it had just started (up to ~1.2 ms per
       10 ms) inside the same loop that must re-arm the DAC. */
    if (tx_busy && dma_poll(DMA_ETH_BASE, DMA_TX) == 0) {
        (void)dma_wait(DMA_ETH_BASE, DMA_TX, 200000u);
        g_txstall++;
    }
    tx_busy = 0;

    memset(e, 0, 64);
    memcpy(e, bcast, 6);
    e[6] = MAC0; e[7] = 0x00; e[8] = 0x00; e[9] = 0x00; e[10] = 0x00; e[11] = MAC5;
    e[12] = 0x08; e[13] = 0x00;

    ip = e + 14;
    ip[0] = 0x45; ip[1] = 0x00;
    ip[2] = (u8)(total >> 8); ip[3] = (u8)total;
    ip[4] = 0x00; ip[5] = 0x01;
    ip[6] = 0x40; ip[7] = 0x00;
    ip[8] = 64; ip[9] = 17;
    ip[10] = 0; ip[11] = 0;
    ip[12] = BOARD_IP0; ip[13] = BOARD_IP1; ip[14] = BOARD_IP2; ip[15] = BOARD_IP3;
    ip[16] = 255; ip[17] = 255; ip[18] = 255; ip[19] = 255;
    c = ip_csum(ip, 20);
    ip[10] = (u8)(c >> 8); ip[11] = (u8)c;

    udp = ip + 20;
    udp[0] = (u8)(port >> 8); udp[1] = (u8)port;
    udp[2] = (u8)(port >> 8); udp[3] = (u8)port;
    udp[4] = (u8)((16u + n) >> 8); udp[5] = (u8)(16u + n);
    udp[6] = 0; udp[7] = 0;

    udp[8]  = (u8)t0;           udp[9] = (u8)t1;
    udp[10] = (u8)(seq >> 8);   udp[11] = (u8)seq;
    udp[12] = (u8)(chunk >> 8); udp[13] = (u8)chunk;
    udp[14] = (u8)(lenfield >> 8); udp[15] = (u8)lenfield;
    memcpy(udp + 16, payload, n);

    flen = 14u + total;
    Xil_DCacheFlushRange((UINTPTR)txbuf, flen);
    dma_start_nr(DMA_ETH_BASE, DMA_TX, (u32)(UINTPTR)txbuf, flen);
    tx_busy = 1;
}

/* Audio uplink: the 'A''U' frame the PC console already knows. */
static void send_udp(const u8 *payload, u32 n, u32 seq, u32 chunk, u32 port)
{
    send_frame(payload, n, 'A', 'U', seq, chunk, AUDIO_BYTES, port);
}

/* Status uplink ('A''S', 15 bytes, payload version 2) -- sent to PORT_STAT every
 * 250 ms so the PC can display the board's real state.  Field order is mirrored
 * in work/pc_app/web_server.py:parse_status(); keep the two in sync.
 *
 * v2 (2026-11-02) added noise (底噪注入电平) and dac_mode (双 DAC 模式) --
 * 这两项原来只能看串口，界面只能显示"本机意图"，属于"点了看不出效果"的来源。
 * The body lives further down, after the globals it reports are declared. */
static u32 g_stat_seq = 0;

static void send_status(void);

/* ---------------- PHY: 100 Mbps full duplex via auto-negotiation ---- */
static void mdio_init(void) { Xil_Out32(ETH_BASE + 0x500, 0x40u | 0x1Fu); usleep(1000); }

static void mdio_wr(u32 phy, u32 reg, u32 val)
{
    u32 mcr = ((phy & 0x1Fu) << 24) | ((reg & 0x1Fu) << 16) | 0x00004000u;
    u32 t = 0;

    Xil_Out32(ETH_BASE + 0x508, val & 0xFFFFu);
    Xil_Out32(ETH_BASE + 0x504, mcr | 0x800u);
    while (!(Xil_In32(ETH_BASE + 0x504) & 0x80u) && ++t < 1000000u) { }
}

static u32 mdio_rd(u32 phy, u32 reg)
{
    u32 mcr = ((phy & 0x1Fu) << 24) | ((reg & 0x1Fu) << 16) | 0x00008000u;
    u32 t = 0;

    Xil_Out32(ETH_BASE + 0x504, mcr | 0x800u);
    while (!(Xil_In32(ETH_BASE + 0x504) & 0x80u) && ++t < 1000000u) { }
    return Xil_In32(ETH_BASE + 0x50C) & 0xFFFFu;
}

/* ---------------- audio helpers ---------------- */
/* 440 Hz self-test tone.  The table has 64 entries, so one sample advances the
   table by 64*440/48000 = 0.5867 entries.  `phase` is 16.16 fixed point, so
   that increment is 0.5867*65536 = 38435 and the entry index is (phase >> 16).
   This used to shift by 10, i.e. 37.5 entries per sample = 0.586 of a CYCLE per
   sample = 28.1 kHz, which at 48 kHz aliases to 19.86 kHz -- i.e. the "440 Hz"
   self-test tone was inaudible, and anything measured about it was garbage. */
static void fill_tone(u32 *dst, u32 words, u32 *phase)
{
    u32 i;

    for (i = 0; i < words; i++) {
        s16 s = sine64[(*phase >> 16) & 63u];
        s32 v = (s32)s;
        dst[i] = ((u32)((u16)v) & 0xFFFFu) | (((u32)((u16)v) & 0xFFFFu) << 16);
        *phase += 38435u;                       /* 64 * 440 / 48000 in 16.16 */
    }
}

/* Output level for the DAC path.  The PS-side route has no attenuator: the
   samples go network -> pbuf -> DMA -> I2S with nothing in between, so the only
   place a volume can live is here, scaling the packed {R,L} words before the
   DMA is armed.  Q15 on purpose: 32768 is exact unity, and the scaling runs
   once per completed 100 ms buffer, not per sample in the streaming path.
   The 'v' control byte rots through four steps (see the ctl branch). */
static void apply_gain(u32 *w, u32 n)
{
    u32 i;
    int l, r;

    if (g_gain == 32768) return;                /* unity: touch nothing */
    for (i = 0; i < n; i++) {
        l = (int)((s16)(w[i] & 0xFFFFu) * g_gain) >> 15;
        r = (int)((s16)(w[i] >> 16)          * g_gain) >> 15;
        if (l >  32767) l =  32767;
        if (l < -32768) l = -32768;
        if (r >  32767) r =  32767;
        if (r < -32768) r = -32768;
        w[i] = ((u32)((u16)r) << 16) | ((u32)((u16)l) & 0xFFFFu);
    }
}

/* The AXI DMA simple-mode transfer length is limited by c_sg_length_width
   (default 14 bits): writing 19200 to R_LEN reads back 0x0B00 = 2816, i.e.
   19200 - 16384, so 85% of every 100 ms buffer was silently dropped.  Feed the
   I2S channel in chunks that stay below 16384 bytes instead. */
#define PB_CHUNK_WORDS 1200u    /* 4800 B; PB_WORDS / PB_CHUNK_WORDS == 4 */

/* ---------- on-device denoise (RNNoise, 480 samples @ 48 kHz) ----------
 * rnnoise_process_frame() takes and returns *int16 units* (-32768..32767), not
 * normalised floats.  Do not pre-scale the input: an early version multiplied
 * by RN_PRE = 4 to "match the training range", which saturated the internal
 * filters and made the output silent.  MIC_GAIN is applied on the way out so
 * the denoised branch is level-matched to bypass.
 * Mode is latched from the PS UART: '0' = bypass, '1' = RNNoise, '2' = gate. */
#define UART0_SR   0xE000002Cu
#define UART0_FIFO 0xE0000030u

static DenoiseState *g_st;
static float g_fin[FRAME_WORDS];
static float g_fout[FRAME_WORDS];
static u32 g_dsp_mode;          /* 0 = bypass, 1 = RNNoise, 2 = gate, 3 = PL */
static u32 g_dac_mode;          /* 包6：双 DAC 输出模式 0=M0 / 1=M1 / 2=M2   */
static u32 g_tone_en;           /* 0 = stay silent when idle (default)      */
static u32 g_dsp_ticks;         /* cost of the last denoised frame, in ticks */

/* ---------- mode 1: RNNoise, batched (latency traded for quality) ---------
 * Until 2026-10-29 mode 1 ran RNNoise *inside* the 10 ms frame callback: the
 * uplink had 10 ms of wall time per frame and the network needed 20.07, so
 * every second frame was lost.  Measured on three 10 s recordings
 * (材料库/04-实测记录/音频样本/2026-10-27_三档语音对比): 1000 bypass frames,
 * 486 in RNNoise -- i.e. 4.86 s of audio out of a 10 s take.  That is exactly
 * the "chopped / skipping" complaint.
 *
 * The fix is not to make RNNoise faster, it is to stop pretending it is a
 * real-time operator on a 10 ms grid.  We now let frames accumulate and let
 * the network push them out whenever the CPU gets there:
 *
 *   input  10 ms frame  ->  append to g_bq (a 512-frame ring = 5.12 s)
 *   output 10 ms frame  <-  pop from g_bq whenever the uplink is ready
 *
 * Nothing is ever dropped: the network simply becomes a delayed player.  The
 * uplink rate stays at 100 fps *in the mean*, but because a whole batch is
 * produced in one burst, the instantaneous rate is bursty and the PC sees a
 * few frames early and a few late.  That is the latency we are buying quality
 * with:
 *
 *   backlog = input rate (100 fps) - RNNoise rate (~50 fps)
 *           -> the ring fills at ~50 frames per second
 *
 * RN_BATCH_TARGET is the point where the backlog stops: from then on the
 * uplink drains it as fast as the CPU fills it, which keeps the backlog (=
 * added latency) bounded at RN_BATCH_TARGET frames = 300 ms.  Measured
 * per-frame cost on this board: 20.07 ms, so 30 frames of work = 602 ms of
 * CPU per batch of 300 ms of audio.
 *
 * When the mode is selected the ring is empty, so the first few hundred ms
 * are prefilled with the frames already sitting in the input ring -- the
 * player starts at once and never underruns.  On leaving the mode the ring is
 * flushed and the PC-side timeline simply jumps forward once (the PC's
 * jitter buffer must be >= this, otherwise it will not notice).
 *
 * Bypass (mode 0) and gate (mode 2) keep the original no-latency path. */
#define RN_BATCH_TARGET 30      /* frames of backlog to hold, 10 ms each     */
#define RN_Q_FRAMES     512     /* 5.12 s ring; must be a power of two       */
#define RN_Q_MASK       (RN_Q_FRAMES - 1)

/* TWO rings, not one.  This is the fix for the 2026-10-29 "RNNoise goes
   completely silent" bug.
 *
 * The first version had a single ring with a read index that was moved twice:
 * once by the batch loop that denoised in place, once by the sender that
 * drained it.  The two disagreed -- the sender walked over frames the batch
 * loop had not touched yet, so it transmitted *raw* frames, and the fill
 * count was decremented by both, so the "> target" test was almost never
 * true and the uplink stalled.  Heard as: silence, and counters that made no
 * sense (qfill jumping by hundreds of thousands between two reads).
 *
 * Now the two stages are physically separate:
 *
 *   g_rq_in  : raw MIC frames waiting to be denoised   (writer: MIC callback)
 *   g_rq_out : already-denoised frames waiting to be sent (writer: batch loop)
 *
 * One writer and one reader per ring, and the reader never moves the writer's
 * index.  There is nothing left to disagree about. */
static u32 g_rq_in[RN_Q_FRAMES][FRAME_WORDS]  __attribute__((aligned(64)));
static u32 g_rq_out[RN_Q_FRAMES][FRAME_WORDS] __attribute__((aligned(64)));
static u32 g_in_w,  g_in_r;     /* raw ring:  written by MIC, read by batch   */
static u32 g_out_w, g_out_r;    /* done ring: written by batch, read by send  */
static u32 g_in_fill, g_out_fill;
static u32 g_rn_batches;        /* completed batches, for the console         */
static u32 g_qmax;              /* deepest backlog seen (overflow evidence)   */
static u32 g_qdrops;            /* frames lost to a full ring                 */
static u32 g_rn_target = RN_BATCH_TARGET;   /* ladder value, 'i' / 'j' bytes  */
/* INTERIM QUALITY FIX (2026-10-30).  The denoiser manages ~49 frames/s while
   the mic produces 100, so the backlog throttle in stage 3 could only transmit
   half the audio; the PC conceals the other half with silence, which is what
   made mode 1 sound "almost inaudible".  With g_rn_pass set, every second
   frame bypasses the denoiser and goes out raw, so the uplink stays continuous
   at ~100 fps: half the frames are denoised, half are untouched.  Set to 0 to
   restore the old drop-half behaviour.  g_pass_n counts the bypassed frames. */
static u32 g_rn_pass = 1u;
static u32 g_rn_alt;
static u32 g_pass_n;
/* Independent delay on/off.  0 = delay active (default, unchanged behaviour),
   1 = zero added delay.  Kept separate from the ladder so toggling it does not
   destroy the operator's chosen depth.  See num_set_lat_bypass(). */
static u32 g_lat_off;

/* ---------- time scaling, 50..100 % ---------------------------------------
 * WHAT IT IS
 * Mode 1 (RNNoise) has a hard cost ceiling: 20.07 ms per 10 ms frame on this
 * A9, so 100 fps input cannot be denoised at 100 fps.  Two honest responses:
 *   A) drop frames   -> 10 s of speech becomes 4.86 s of audio ("chopped")
 *   B) stretch time  -> keep every sample, let the take play back slower
 * This block implements B with a *tunable* ratio instead of a hard-coded 50 %:
 *
 *   rate = 100  -> no change at all (memcpy path, zero extra latency)
 *   rate =  50  -> half speed, take twice as long, nothing lost
 *   rate =  75  -> three-quarter speed, take 1.33x longer
 *
 * WHY IT IS A RATE AND NOT A SWITCH
 * A fixed 50 % is a very blunt instrument -- it is only justified when the
 * denominator really is ~2 (100 fps against ~50 fps).  Measured throughput
 * varies with the noise and the load, so the operator needs a dial.  The
 * default is 100 = "behave exactly as before": nothing in the existing
 * pipeline may change unless someone asks for it.
 *
 * WHERE IT SITS
 * Applied to the RNNoise output frames, after rn_frame() and before they are
 * queued for transmission.  It therefore never touches the DENOISED content
 * itself, only the rate at which the same content is delivered.  Modes 0 and 2
 * are far below the CPU budget (0.127 ms) and never need it.
 *
 * VALIDATION
 * parse_ts_rate() rejects anything outside 50..100 with an explicit message,
 * and the caller does not change the stored value.  Silent clamping was
 * rejected on purpose: clamping hides operator error and makes "I asked for
 * 30 % and got 50 %" indistinguishable from "30 % is broken".
 * ------------------------------------------------------------------------- */
static ts_state g_ts;
static u32      g_ts_rate = TS_RATE_DEF;   /* 50..100, percent             */
static u32      g_ts_apply;                /* 0 = stretch disabled (rate 100)*/
static u32      g_ts_rejects;              /* out-of-range requests, for tlm */

static const char *dsp_name(u32 m)
{
    return (m == 1u) ? "RNNOISE" : (m == 2u) ? "GATE" :
           (m == 3u) ? "PL-SS"   : "BYPASS";
}

/* Synthetic noise injected into the microphone path.  The demo room is so
   quiet (about -59 dBFS) that RNNoise has nothing to remove, so an A/B there
   shows ~0.1 dB and looks like the denoiser is dead.  The noise is added
   *before* the mode branch so both branches see exactly the same input.
 *
   It is deliberately LOW-PASSED (leaky integrator, ~500 Hz corner) instead of
   white: RNNoise classifies broadband hiss as unvoiced speech and passes it
   through almost untouched (measured: -0.0 dB on white noise), while low
   frequency rumble is exactly the kind of stationary noise it removes.  White
   noise therefore produced a useless A/B even though the denoiser was fine. */
static u32 g_noise;             /* 0 = off, else target rms in LSB         */
static u32 g_rnd = 0x13572468u;
static s32 g_nz_l, g_nz_r;      /* integrator state, one per channel       */

/* ---------- AXI GPIO 通道2 打包写（2026-11-02 方案A） ---------------------
   BD 里通道2 的位宽已从 2 扩到 4，并用两个 xlslice 拆成两段：

       bit[1:0] -> dac_split_0/mode   双 DAC 输出模式 M0/M1/M2
       bit[3:2] -> dn_axis_0/nz       PL 侧底噪注入档位 0/1/2/3

   两段在硬件里是同一个寄存器，所以必须**一起**写。以前只写 mode（&3）时
   噪声位恒为 0；只写噪声位时 DAC 模式会被打回 M0 —— 上板表现为"调底噪
   会把双 DAC 模式重置"，这类 bug 很难从现象追回来，所以只留这一个入口。 */
static u32 noise_bits_of(u32 nz_lsb)
{
    return (nz_lsb == 0u) ? 0u : (nz_lsb <= 300u) ? 1u :
           (nz_lsb <= 800u) ? 2u : 3u;
}

/* 写进 GPIO 通道2 的那 4 位，能不能原样读回来？
 * 读回来不一致，说明这块 bit 里通道2 不是我们的输出寄存器（例如 bit 里根本没有
 * dac_split / 通道2 还是输入），此时 dac_split 收不到 mode，切了也不会有任何变化。 */
static u32 g_gpio2_rb;      /* 最近一次读回值（低 4 位） */
static u32 g_gpio2_ok = 1u; /* 1 = 写读一致（PL 侧确实接上了） */
static u32 g_gpio2_present; /* 1 = 上电探测确认过：写 1 能读回 1 */

/* 上电探测：只靠"写什么读回什么"是不够的 —— M0 写下去是 0，而通道2 若不是我们的
 * 输出寄存器，浮空/输入也可能读到 0，于是 M0 永远"看起来正常"（2026-11-02 实测：
 * 10-29 那版根本没有 dac_split，控制台却一直显示 M0 同源）。
 * 所以先写一个非零值 1 读回来核对一次。写 1 只维持几个 50 MHz 周期（等价于瞬间
 * 切到 M1 左右分离），人耳听不出来；随后立刻写回 0。 */
static void hw_gpio2_probe(void)
{
    Xil_Out32(GPIO_MODE_BASE + 4u, 0u);   /* TRI=0 全部输出 */
    Xil_Out32(GPIO_MODE_BASE, 1u);
    g_gpio2_present = ((Xil_In32(GPIO_MODE_BASE) & 0xFu) == 1u) ? 1u : 0u;
    Xil_Out32(GPIO_MODE_BASE, 0u);
}

static void hw_gpio2_apply(void)
{
    u32 v = (g_dac_mode & 3u) | ((noise_bits_of(g_noise) & 3u) << 2);
    Xil_Out32(GPIO_MODE_BASE + 4u, 0u);  /* TRI=0 全部输出 */
    Xil_Out32(GPIO_MODE_BASE, v);
    g_gpio2_rb = Xil_In32(GPIO_MODE_BASE) & 0xFu;
    g_gpio2_ok = ((g_gpio2_rb == (v & 0xFu)) && (g_gpio2_present != 0u)) ? 1u : 0u;
}

static s32 noise_sample(void)
{
    g_rnd = (g_rnd >> 1) ^ ((0u - (g_rnd & 1u)) & 0xB400u);
    return (s32)(g_rnd & 0xFFFFu) - 32768;   /* uniform in [-32768, 32767) */
}

static s32 noise_lowpassed(s32 *state)
{
    s32 w = noise_sample() >> 5;             /* scale before the integrator */
    *state += w - (*state >> 5);             /* one pole at 1/32 -> ~240 Hz  */
    return *state;
}

/* ---------- mode 2: lightweight adaptive noise gate (route A) --------------
 * Why this exists: RNNoise needs 20.07 ms to process a 10 ms frame on this
 * 667 MHz dual A9, so it can only run on every other frame and the uplink
 * collapses from 100 fps to 49 fps — heard as "chopped".  An FFT-based method
 * cannot be squeezed into a 10 ms budget on this part, so the real-time path
 * has to use a different cost model.
 *
 * This gate only scales the amplitude of the whole frame; it never touches the
 * spectrum and never rebuilds phase, so it costs tens of microseconds, adds no
 * latency, and cannot produce the metallic timbre a spectral suppressor does.
 *
 * One gain per 10 ms frame, then a per-sample one-pole so the frame boundary
 * cannot click.  GATE_MIN is -24 dB instead of 0 on purpose: the classic
 * "breathing" complaint comes from the background hitting absolute silence, so
 * the gate rides the floor down instead of muting. */
#define GATE_THR   3.2f    /* fully open at 10 dB above the tracked floor  */
#define GATE_OPEN0 1.6f    /* still shut until 4 dB above it               */
#define GATE_MIN   0.06f   /* -24 dB, never absolute silence               */
#define GATE_ATK   0.5f    /* per-sample smoothing when opening            */
#define GATE_REL   0.05f   /* per-sample smoothing when closing            */
#define GATE_PRIME 30u     /* 300 ms of listening to seed the floor        */

static float g_gate_nf = 8.0f;   /* tracked noise floor, LSB rms         */
static float g_gate_g  = 1.0f;   /* smoothed per-sample gain             */
static float g_gate_sum;         /* rms accumulator during priming       */
static u32   g_gate_prime;       /* frames listened to since mode switch */

static void gate_reset(void)
{
    g_gate_nf = 8.0f;
    g_gate_g  = 1.0f;
    g_gate_sum = 0.0f;
    g_gate_prime = 0u;
}

/* ---------- output delay line, shared by modes 0 and 2 --------------------
 * The 'i' / 'j' control bytes and the console's two latency buttons have to
 * do something in EVERY mode, not just in RNNoise -- otherwise switching to
 * the gate silently drops the delay the operator just dialled in, and the two
 * modes can no longer be compared at equal latency.
 *
 * Modes 0 and 2 are cheap, so they are not batched.  Instead they run through
 * this plain FIFO, which holds g_dly_frames frames (0..90, i.e. 0..900 ms)
 * and hands them back in order.  RNNoise (mode 1) gets its delay from the
 * batch rings instead, so it does NOT use this on the way in -- but it does
 * expose the same g_rn_target number, which is what makes "300 ms in gate"
 * and "300 ms in RNNoise" mean the same thing. */
#define DLY_FRAMES 96           /* 960 ms ceiling, room for the 90-frame step */
#define DLY_MASK   (DLY_FRAMES - 1)

static u32 g_dly[DLY_FRAMES][FRAME_WORDS] __attribute__((aligned(64)));
static u32 g_dly_w, g_dly_r;
static u32 g_dly_fill;
static u32 g_dly_frames;        /* 0 = no added delay (default for 0 / 2)    */

/* Status uplink body: every field here is what the operator can change, so a PC
   that listens on PORT_STAT can show the board's truth instead of its own last
   guess.  p[14] is an XOR of the first 14 bytes; a receiver that sees a bad XOR
   drops the packet rather than display a half-parsed state.
   v2 adds noise (p[11..12]) and dac_mode (p[13]); a v1 parser still works because
   it only reads the first 12 bytes and its XOR covers p[0..10] -- 但 v1 端的校验
   会和 p[11] (noise_hi) 对不上，所以收到 v2 的 v1 解析器会整包丢弃，宁可没有
   状态显示，也不显示错位状态。 */
static void send_status(void)
{
    u8 p[15];
    u32 i, x = 0;

    p[0]  = 'S';                    /* payload tag              */
    p[1]  = 2u;                     /* payload version          */
    p[2]  = (u8)g_dsp_mode;         /* 0 bypass / 1 RNNoise / 2 gate / 3 PL */
    p[3]  = (u8)g_ts_rate;          /* 50..100 %                */
    p[4]  = (u8)g_ts_apply;         /* stretch request pending   */
    p[5]  = (u8)g_dly_frames;
    p[6]  = (u8)g_lat_off;
    p[7]  = (u8)(g_gain >> 8);
    p[8]  = (u8)g_gain;
    p[9]  = (u8)g_rn_target;
    p[10] = (u8)g_tone_en;
    p[11] = (u8)(g_noise >> 8);     /* 底噪注入 0/300/800/1500  */
    p[12] = (u8)g_noise;
    /* 读回不一致时报 3：调用方据此知道"模式没接到 PL"，别再显示成已生效 */
    p[13] = (u8)((g_gpio2_ok != 0u) ? g_dac_mode : 3u);
    for (i = 0; i < 14u; i++) x ^= p[i];
    p[14] = (u8)x;

    send_frame(p, sizeof(p), 'A', 'S', g_stat_seq++, 0u, (u32)sizeof(p), PORT_STAT);
}

static void gate_flush_reset(void)
{
    g_dly_r = g_dly_w;
    g_dly_fill = 0u;
}

/* Push a processed frame and, if the line is deep enough, hand back the frame
   that is now g_dly_frames old.  Returns 0 while the line is still filling. */
static u32 *delay_push_pop(u32 *mb)
{
    u32 *out = 0;

    if (g_dly_frames == 0u) return mb;          /* no delay: pass through    */
    if (g_dly_fill < DLY_FRAMES) {
        memcpy(g_dly[g_dly_w & DLY_MASK], mb, AUDIO_BYTES);
        g_dly_w++;
        g_dly_fill++;
    }
    if (g_dly_fill > g_dly_frames) {
        out = g_dly[g_dly_r & DLY_MASK];
        g_dly_r++;
        g_dly_fill--;
    }
    return out;
}


static void gate_apply(u32 *mb)
{
    int k;
    float sum = 0.0f, rms, snr, open, g;

    for (k = 0; k < (int)FRAME_WORDS; k++) {
        float l = (float)(s32)(s16)(mb[k] & 0xFFFFu);
        float r = (float)(s32)(s16)((mb[k] >> 16) & 0xFFFFu);
        sum += l * l + r * r;
    }
    rms = sqrtf(sum / (float)(2u * FRAME_WORDS));

    if (g_gate_prime < GATE_PRIME) {
        /* Priming.  Starting the floor at a fixed guess does not work: if the
           guess is low the gate needs ~20 s to creep up to the real floor and
           stays wide open (measured 2.8 dB of suppression instead of 24), and
           if the guess is high it eats the talker.  Listening for 300 ms and
           seeding from the average is free here because a mode switch is
           already a 100 ms-scale event.  If the user talks during those 300 ms
           the seed lands too high, which fails safe: the gate just stays open. */
        g_gate_sum += rms;
        if (++g_gate_prime == GATE_PRIME) {
            g_gate_nf = 1.4f * (g_gate_sum / (float)GATE_PRIME);
            if (g_gate_nf < 1.0f) g_gate_nf = 1.0f;
            xil_printf("gate floor=%d\r\n", (int)g_gate_nf);
        }
        g = 1.0f;
    } else {
        /* Floor: drops fast, climbs very slowly *while the gate is open* — that
           is what stops a long sentence from dragging the floor up and clamping
           the talker mid-phrase.  While the gate is shut it may climb quickly,
           because then the frames it hears really are the background; without
           that the tracker sat 50 % below the true rms (measured nf=18..23 vs
           an input near 41), snr hovered at 2 and the gate leaked -15 dB
           instead of the -24 dB it was designed for. */
        open = (rms > g_gate_nf * GATE_THR) ? 1.0f : 0.0f;
        if (rms < g_gate_nf) g_gate_nf += (rms - g_gate_nf) * 0.02f;
        else                 g_gate_nf += (rms - g_gate_nf) * (open ? 0.0005f : 0.002f);
        if (g_gate_nf < 1.0f) g_gate_nf = 1.0f;

        snr = rms / (g_gate_nf + 1e-6f);
        /* The floor tracker is deliberately asymmetric (drops fast, climbs
           slowly), so it settles BELOW the mean frame level — measured 30 %
           below with the low-passed demo noise, where a 10 ms frame only holds
           ~15 independent samples and its energy swings by 18 %.  Mapping
           snr 1..THR straight onto 0..1 therefore left the gate 7 dB ajar
           (-17 dB instead of -24).  The gate is now held shut until snr
           exceeds GATE_OPEN0, and only then does it start to open. */
        g = (snr - GATE_OPEN0) / (GATE_THR - GATE_OPEN0);
        if (g < 0.0f) g = 0.0f;
        if (g > 1.0f) g = 1.0f;
        g = g * g * (3.0f - 2.0f * g);      /* smoothstep, no hard knee     */
        g = GATE_MIN + (1.0f - GATE_MIN) * g;
    }

    for (k = 0; k < (int)FRAME_WORDS; k++) {
        float gl, gr;
        s32 il, ir;

        g_gate_g += (g - g_gate_g) * (g > g_gate_g ? GATE_ATK : GATE_REL);
        gl = (float)(s32)(s16)(mb[k] & 0xFFFFu) * g_gate_g * (float)MIC_GAIN;
        gr = (float)(s32)(s16)((mb[k] >> 16) & 0xFFFFu) * g_gate_g * (float)MIC_GAIN;
        il = (s32)gl;
        ir = (s32)gr;
        if (il > 32767) il = 32767;
        if (il < -32768) il = -32768;
        if (ir > 32767) ir = 32767;
        if (ir < -32768) ir = -32768;
        mb[k] = ((u32)((u16)ir) << 16) | ((u16)il & 0xFFFFu);
    }
}

/* One frame through RNNoise, in place.
 *
 * SCALING BUG FIXED 2026-10-29 (this is why RNNoise was completely silent).
 * rnnoise_process_frame() works in *int16 units*: it expects in[] and returns
 * out[] in the -32768..32767 range (the upstream example divides raw samples
 * by 32768 only when it feeds a WAV writer, never before the network).  The
 * old code multiplied the samples by RN_PRE = 4 "to match the -6..-12 dBFS
 * training range", which pushed a normal -20 dBFS signal to +12 dBFS -- 4x
 * past full scale.  The internal high-pass, the FFT and the band-energy
 * features all saturated, the VAD classified every frame as garbage, and the
 * output came out at -90..-104 dBFS, i.e. silence.  Frame rate stayed at
 * 100 fps and no error was ever raised, which is exactly the symptom
 * reported: "切到 RNNoise 就完全没声音".
 *
 * Now: no pre-gain, no post-gain.  The 16-bit value goes straight in and
 * comes straight out, so the denoised branch is level-matched to bypass by
 * construction instead of by balancing two magic constants. */
static void rn_frame(u32 *mb)
{
    int k;
    u32 t0, t1;

    for (k = 0; k < (int)FRAME_WORDS; k++) {
        g_fin[k] = (float)(s32)(s16)(mb[k] & 0xFFFFu);
    }
    t0 = Xil_In32(TIMER_LOW);
    rnnoise_process_frame(g_st, g_fout, g_fin);
    t1 = Xil_In32(TIMER_LOW);
    g_dsp_ticks = t1 - t0;
    for (k = 0; k < (int)FRAME_WORDS; k++) {
        /* Apply the same MIC_GAIN the other two modes apply, but AFTER the
           network and with saturation, so an A/B is level-matched. */
        s32 v = (s32)(g_fout[k] * (float)MIC_GAIN);
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        mb[k] = ((u32)((u16)v) << 16) | ((u16)v & 0xFFFFu);
    }
}

/* Mode 1, stage 1: push one raw MIC frame into the input ring.  This runs
   inside the 10 ms callback and must stay cheap -- it is a memcpy and three
   index updates, nothing else. */
static void rn_push_in(u32 *mb)
{
    if (g_in_fill >= RN_Q_FRAMES) {
        /* Input ring full: the denoiser is not keeping up.  Drop the OLDEST
           raw frame so the newest audio survives -- a listener hears fresh
           audio with a gap rather than 5 s stale audio. */
        g_in_r++;
        g_in_fill--;
        g_qdrops++;
    }
    memcpy(g_rq_in[g_in_w & RN_Q_MASK], mb, AUDIO_BYTES);
    g_in_w++;
    g_in_fill++;
    if (g_in_fill > g_qmax) g_qmax = g_in_fill;
}

/* Mode 1, stage 2: if enough raw frames are waiting, denoise ONE frame and
   move the result into the output ring.  Called from the main loop, never
   from the callback, so its cost never blocks the MIC path.

   *** Why exactly one frame, not the whole batch ***
   The first version denoised all g_rn_target frames in one pass, which is
   what the "process a batch" name suggests.  At 20.07 ms per frame that is
   600 ms inside a single loop iteration: the DMA uplink stops, nothing is
   sent, nothing is polled.  Measured uplink came out at 31.6 fps instead of
   100 -- the frames were not lost, they simply were not transmitted while
   the CPU was busy.

   Denoising one frame per loop pass spreads the same work over many passes.
   The loop is fast enough that it spins ~46 times per second while 100 frames
   arrive per second, so the input ring still fills: the backlog grows until
   it reaches g_rn_target, and the *output* then drips at CPU speed.  Same
   throughput ceiling, but the transmitter keeps running the whole time.

   The output ring only ever receives frames that have actually been through
   rn_frame().  That is the invariant the first version violated. */
static void rn_batch_once(void)
{
    if (g_in_fill == 0u) return;
    if (g_out_fill >= RN_Q_FRAMES) return;      /* no room: wait */

    memcpy(g_rq_out[g_out_w & RN_Q_MASK], g_rq_in[g_in_r & RN_Q_MASK],
           AUDIO_BYTES);
    rn_frame(g_rq_out[g_out_w & RN_Q_MASK]);
    g_in_r++;
    g_out_w++;
    g_in_fill--;
    g_out_fill++;
    g_rn_batches++;
}

/* Mode 1, stage 3: hand back one denoised frame, or NULL if none is ready.
   The caller sends it and moves on; it never inspects the rings itself. */
static u32 *rn_pop_out(void)
{
    u32 *fb;

    if (g_out_fill == 0u) return 0;
    fb = g_rq_out[g_out_r & RN_Q_MASK];
    g_out_r++;
    g_out_fill--;
    return fb;
}

/* Drop both rings -- called on every mode change so a switch never replays
   stale audio and never leaves a half-processed batch behind.
 *
 * The time scaler is flushed with the rings for the same reason: it holds a
 * partial output frame in its own queue, and replaying that after a switch
 * would emit a fragment captured at the old ratio -- audibly a stutter that
 * makes "the mode I picked is not what I hear" true again.  ts_reset() is a
 * memset of ~10 KB, i.e. free at the rate mode switches happen. */
static void rn_flush(void)
{
    g_in_r  = g_in_w;
    g_out_r = g_out_w;
    g_in_fill  = 0u;
    g_out_fill = 0u;
    ts_reset(&g_ts);
}

/* ---------- one latency knob for every mode -------------------------------
 * 'i' / 'j' (and the console's two buttons) walk the same ladder in all three
 * modes, so "300 ms" means 300 ms whether the gate, RNNoise or bypass is
 * selected -- that is what makes the modes comparable at equal delay.
 *
 * RNNoise uses the value as its batch depth; the other two use it as the
 * length of the shared delay line.  Both are set, so a mode switch keeps the
 * latency the operator chose instead of silently resetting it to zero. */
/* 'i' = 300 ms, 'j' = 200 ms -- absolute values, NOT steps.
 *
 * These used to be +1 / -1 on a ladder, which broke as soon as the console
 * went over UDP broadcast: the board receives every control packet two or
 * three times (one per stack retry), so a single click was applied two or
 * three times and the delay jumped past the requested value.  Any command
 * that arrives twice must be a no-op the second time, so the protocol is now
 * "set to X" everywhere, and re-applying the current value returns early. */
static void num_set_latency(u32 frames)
{
    u32 old = g_rn_target;

    if (frames > 90u) frames = 90u;
    if (frames == old) return;          /* duplicate packet: nothing to do   */

    g_rn_target  = frames;
    /* The ladder value is always recorded.  The *effective* depth is only
       applied while the switch is on -- otherwise a step change would
       silently re-enable a delay the operator had turned off. */
    g_dly_frames = g_lat_off ? 0u : frames;

    /* Shrinking the delay must drop buffered audio rather than stall for it;
       growing it just lets the line fill naturally. */
    if (frames < old) {
        rn_flush();
        gate_flush_reset();
    }
    xil_printf("lat=%u frames (%u ms) bypass=%u mode=%u\r\n",
               frames, frames * 10u, g_lat_off, g_dsp_mode);
}

/* ---------- independent delay on/off switch ------------------------------
 * WHAT WAS WRONG
 * The latency dial had no off position, so the only way to hear the board
 * with zero added delay was to stop receiving altogether.  That is not the
 * same thing: stopping the receiver also stops the stream, so the user could
 * not compare "no delay" against "200 ms" without tearing the session down
 * and rebuilding it -- and while it was down there was nothing to listen to.
 *
 * WHY A SEPARATE FLAG, NOT A "0 FRAME" LATENCY STEP
 * Setting the ladder to 0 does not work for RNNoise (mode 1).  There the
 * number is the backlog target that the send gate uses:
 *         if ((g_in_fill + g_out_fill) > g_rn_target) send one frame
 * With target 0 that condition is "backlog > 0", which goes false the moment
 * the rings drain -- output stalls.  It only "works" while audio happens to
 * be arriving faster than it leaves, i.e. it is a race, not a setting.
 *
 * So the switch is a SEPARATE boolean that every consumer checks explicitly:
 *   - g_dly_frames -> 0     : bypass/gate delay line passes through untouched
 *   - mode 1            : the send gate is bypassed; frames go out as soon as
 *                         they are denoised, so the only latency left is the
 *                         unavoidable one (the CPU cannot denoise at 100 fps
 *                         anyway: 20.07 ms per frame means ~49 fps).
 *   - g_dly / rn rings  : flushed on every change, so no stale audio replays.
 *
 * The ladder value (20 / 30 / ...) is KEPT while the switch is off.  Turning
 * the switch back on restores exactly what the operator had dialled in --
 * that is what makes on/off orthogonal to the latency step instead of being
 * a second way to say the same thing. */
static void num_set_lat_bypass(u32 off)
{
    off = off ? 1u : 0u;
    if (off == g_lat_off) return;        /* idempotent: broadcast duplicate   */

    g_lat_off = off;

    /* Bring the effective depths in line with the switch.  The ladder itself
       (g_rn_target_ladder) is untouched, so nothing is lost. */
    g_dly_frames = off ? 0u : g_rn_target;

    /* Whatever is buffered was captured under the previous setting; playing
       it back now would produce a burst of old audio at the new latency. */
    rn_flush();
    gate_flush_reset();

    xil_printf("lat_off=%u lat=%u frames (%u ms) mode=%u\r\n",
               g_lat_off, g_rn_target, g_rn_target * 10u, g_dsp_mode);
}

/* ---------- time-scaling ratio: validation and application ----------------
 * One place where the ratio is validated, clamped-free and reported.  Both
 * the PS console and the UDP control path come through here, so a value is
 * accepted or rejected identically no matter how it arrives (the 2026-10-29
 * lesson about set_dsp_mode applies to every parameter, not just the mode).
 *
 * Rejection is EXPLICIT, not silent:
 *   - the stored rate is unchanged, so a typo cannot degrade the stream
 *   - g_ts_rejects is bumped, so the console can see it over telemetry
 *   - one line is printed with the offending value and the legal range
 * Silent clamping was rejected deliberately.  "I asked for 30 and got 50" and
 * "30 is broken" look identical to a listener, and the second one costs an
 * afternoon to disprove.  An out-of-range request is operator error and should
 * read like one. */
static int ts_parse_rate(u32 pct)
{
    if (pct < TS_RATE_MIN || pct > TS_RATE_MAX) {
        g_ts_rejects++;
        xil_printf("ts=%u REJECTED (legal %u..%u), keeping %u\r\n",
                   pct, (u32)TS_RATE_MIN, (u32)TS_RATE_MAX, g_ts_rate);
        return 0;
    }
    if (pct == g_ts_rate) return 1;             /* idempotent: broadcast dup */

    g_ts_rate  = pct;
    g_ts_apply = (pct < TS_RATE_MAX) ? 1u : 0u;

    /* The scaler is stateful (it holds a partial output frame).  Changing the
       ratio mid-stream without a reset would replay or drop the leftover from
       the previous ratio -- the same class of bug as switching modes without
       flushing the batch rings.  Reset is cheap (one memset of ~10 KB). */
    ts_reset(&g_ts);

    xil_printf("ts=%u%% out/frame=%u %s\r\n",
               g_ts_rate, (u32)ts_out_len((int)g_ts_rate),
               g_ts_apply ? "STRETCH" : "OFF");
    return 1;
}

static u32 *mic_process(u32 *mb)
{
    int k;

    /* PS 侧底噪注入。方案A（2026-11-02）之后噪声有两个注入点：
         PL：dn_axis_0 的 LFSR，GPIO 通道2 bit[3:2] 选档，位置在
             **写入口 FIFO 之前**，bypass=1 直通时也照样叠加；
         PS：下面这段低通积分器，在 DMA 收完一帧之后。
       规则：
       - mode 3 = PL 谱减。噪声必须由 PL 注入、真的经过谱减滤波器，
         所以这里**不能**再注一遍（否则噪声落在滤波器后面，测出来
         floor 不降，A/B 直接失效）。
       - mode 0/1/2 照旧在 PS 侧注入。注意此时 PL 那一路也在叠加
         （nz 位与 bypass 无关），所以噪声是"PL 白噪 + PS 低通"两份叠加，
         只影响响度、不影响这些模式的结论；RNNoise(mode 1) 之所以保留
         PS 的低通注入，是因为它会把宽带白噪当清音放过去（见上面
         g_noise 的注释）。要变成单点注入，把这里整段删掉即可。 */
    if (g_noise && g_dsp_mode != 3u) {
        for (k = 0; k < (int)FRAME_WORDS; k++) {
            s32 lv = (s16)(mb[k] & 0xFFFFu);
            s32 rv = (s16)((mb[k] >> 16) & 0xFFFFu);
            lv += noise_lowpassed(&g_nz_l) * (s32)g_noise / 18000;
            rv += noise_lowpassed(&g_nz_r) * (s32)g_noise / 18000;
            if (lv > 32767) lv = 32767;
            if (lv < -32768) lv = -32768;
            if (rv > 32767) rv = 32767;
            if (rv < -32768) rv = -32768;
            /* low word = left, high word = right, same order as every other
               branch — this line used to swap them (inaudible only because the
               electret feeds both channels identically) */
            mb[k] = ((u32)((u16)rv) << 16) | ((u16)lv & 0xFFFFu);
        }
    }

    if (g_dsp_mode == 2u) {
        u32 t0 = Xil_In32(TIMER_LOW);
        gate_apply(mb);
        g_dsp_ticks = Xil_In32(TIMER_LOW) - t0;
    } else if (g_dsp_mode != 1u) {
        /* bypass.  Mode 1 is *not* handled here any more: RNNoise lives in
           rn_push_in() above, outside the 10 ms callback. */
        for (k = 0; k < (int)FRAME_WORDS; k++) {
            s32 l = (s16)(mb[k] & 0xFFFFu) * MIC_GAIN;
            s32 rr = (s16)((mb[k] >> 16) & 0xFFFFu) * MIC_GAIN;
            if (l > 32767) l = 32767;
            if (l < -32768) l = -32768;
            if (rr > 32767) rr = 32767;
            if (rr < -32768) rr = -32768;
            mb[k] = ((u32)((u16)rr) << 16) | ((u16)l & 0xFFFFu);
        }
    }

    /* Modes 0 and 2 run through the shared delay line so the latency knob
       means the same thing in every mode.  Returns NULL while the line is
       still filling up; the caller simply sends nothing that frame.
       With the delay switch off the line is bypassed entirely (and
       g_dly_frames is already 0, so delay_push_pop would pass through
       anyway -- the explicit test documents the intent and survives any
       future change to how g_dly_frames is maintained). */
    if (g_lat_off) return mb;
    return delay_push_pop(mb);
}

/* ---------- the ONE place a mode change is applied -----------------------
 * There used to be two copies of this block: one on the PS console (above)
 * and one on the UDP control port (below, in the packet handler).  They had
 * already drifted -- UDP only did "g_dsp_mode = n; gate_reset()" and skipped
 * the ring flush, the RNNoise-state check and the counter reset.  So a mode
 * selected from the console behaved differently from the same mode selected
 * from the app, which is exactly the "the mode I picked is not what I hear"
 * symptom.  Both callers now come through here. */
static void set_dsp_mode(u32 m)
{
    u32 old = g_dsp_mode;

    /* Mode 3 = PL spectral subtraction.  The ARM does nothing in that mode:
       the FPGA sits between the I2S receiver and the DMA, so the stream the
       ARM forwards is already denoised. */
    if (m > 3u) return;

    if (m == 1u && !g_st) {
        /* RNNoise selected but its state never initialised: refuse the switch
           instead of going silent.  Reported, not silent. */
        if (old != 1u) xil_printf("rnnoise state missing, staying %u\r\n", old);
        return;
    }

    g_dsp_mode = m;
    if (m == 2u) gate_reset();

    /* PL 谱减的开关（AXI GPIO @0x41200000 bit0，BD 里接到 dn_axis_0/bypass）：
       bypass=1 直通、bypass=0 才做谱减。只有 mode 3 才让 PL 干活 ——
       mode 1（RNNoise 在 ARM 上跑）必须直通，否则同一段音频被压两遍。
       GPIO 复位值是 0（= 谱减开），所以每个模式都要显式写一次。 */
    Xil_Out32(GPIO_BASE, (m == 3u) ? 0u : 1u);

    /* A switch always restarts the stream cleanly: dropping the batch ring
       stops stale audio from being replayed, and the counters are per-run so
       the console reports this selection only. */
    if (m != old) {
        rn_flush();
        g_qmax = 0u;
        g_qdrops = 0u;
        g_rn_batches = 0;
        gate_flush_reset();
    }
    xil_printf("dsp=%u %s\r\n", g_dsp_mode, dsp_name(g_dsp_mode));
}

/* non-blocking look at the PS console: '0' bypass, '1' RNNoise, '2' gate
 *
 * NOTE ON THE LOOP CONDITION (checked 2026-10-30, do not "fix" it again):
 * Zynq UART0_SR bit0 is RXFIFO_EMPTY -- it reads 1 when the FIFO is EMPTY.
 * So "data available" is !(SR & 1), and `while (!(SR & 1)) { read }` means
 * "keep reading while there is something to read", which is exactly right.
 * An earlier session flagged this as inverted on the assumption that bit0
 * meant "data ready"; it does not.  Adding a `break` to "read one byte per
 * call" would silently cap throughput at one byte per 10 ms frame, i.e.
 * ~100 B/s, and a burst of console input would be trivially overrun.
 */
static void mic_mode_poll(void)
{
    while (!(Xil_In32(UART0_SR) & 0x1u)) {   /* bit0=RXFIFO_EMPTY: 1 = empty */
        u8 c = (u8)(Xil_In32(UART0_FIFO) & 0xFFu);
        if (c >= '0' && c <= '3') {
            set_dsp_mode((u32)(c - '0'));
        } else if (c == 't' || c == 'T') {
            /* explicit on/off instead of a toggle, so the same byte can be
               repeated safely (console keys are typed, not broadcast, but one
               protocol for both paths is one less mismatch to debug) */
            g_tone_en = (c == 't') ? 1u : 0u;
            xil_printf("tone=%u\r\n", g_tone_en);
        } else if (c == 'i') {
            num_set_latency(30);
        } else if (c == 'j') {
            num_set_latency(20);
        } else if (c == 'k') {
            num_set_lat_bypass(0);      /* delay ON  (default, unchanged)   */
        } else if (c == 'K') {
            num_set_lat_bypass(1);      /* delay OFF, stream keeps running  */
        } else if (c >= 'e' && c <= 'h') {
            g_gain = (c == 'e') ? 32768 :
                     (c == 'f') ? 24576 :
                     (c == 'g') ? 16384 : 8192;
            xil_printf("gain=%u%% q15=%d\r\n",
                       (unsigned)((g_gain * 100) / 32768), g_gain);
        } else if (c == 'm') {
            /* time scaling up 50 -> 100 in steps of 10 */
            if (g_ts_rate < TS_RATE_MAX) ts_parse_rate(g_ts_rate + 10u);
        } else if (c == 'n') {
            /* time scaling down 100 -> 50 in steps of 10 */
            if (g_ts_rate > TS_RATE_MIN) ts_parse_rate(g_ts_rate - 10u);
        } else if (c == 'p') {
            /* print the current ratio; handy when the console is the only
               channel and the operator wants to confirm what took effect */
            xil_printf("ts=%u%% out/frame=%u apply=%u rejects=%u\r\n",
                       g_ts_rate, (u32)ts_out_len((int)g_ts_rate),
                       g_ts_apply, g_ts_rejects);
        }
    }
}

static void arm_dac(int idx, int off, int n)
{
    Xil_DCacheFlushRange((UINTPTR)&pbuf[idx][off], (u32)n * 4u);
    dma_start_nr(DMA_AUD_BASE, DMA_TX, (u32)(UINTPTR)&pbuf[idx][off], (u32)n * 4u);
    g_t_arm = Xil_In32(TIMER_LOW);
    if (!g_len_rb) g_len_rb = Xil_In32(DMA_AUD_BASE + DMA_TX + R_LEN);
    {
        static int dbg_arm = 0;
        if (dbg_arm < 3) {
            volatile int k;
            g_arm_cr  = Xil_In32(DMA_AUD_BASE + DMA_TX + R_CR);
            g_arm_sr  = Xil_In32(DMA_AUD_BASE + DMA_TX + R_SR);
            for (k = 0; k < 20000; k++) { }
            g_arm_sr2 = Xil_In32(DMA_AUD_BASE + DMA_TX + R_SR);
            xil_printf("arm[%d] cr=%08x sr=%08x spin=%08x\r\n", idx, g_arm_cr, g_arm_sr, g_arm_sr2);
            dbg_arm++;
        }
    }
}

/* ---- DAC MM2S bring-up probe --------------------------------------------
   Everything here only touches registers, so it costs no PL rebuild.  It
   answers three questions in one boot log:
     PRB0  do CR writes read back at all (0x00010003 seen after a soft reset)
     PRB1  does a single-beat (4 byte) MM2S transfer complete
     PRB2  does a full 19200 byte transfer ever assert IOC, and after how long
   SR is a plain status register, so sampling it has no side effects.        */
static u32 g_prb[8];

static void dma_probe(void)
{
    u32 b = DMA_AUD_BASE + DMA_TX;
    u32 v0, v1, v2, v3, v4, v5, v6, v7;
    volatile int k;

    v0 = Xil_In32(b + R_CR);
    v1 = Xil_In32(b + R_SR);
    Xil_Out32(b + R_CR, 0x1u);   v2 = Xil_In32(b + R_CR);   /* RS only */
    Xil_Out32(b + R_CR, 0x0u);   v3 = Xil_In32(b + R_CR);   /* all zero */
    xil_printf("PRB0 cr=%08x sr=%08x rs=%08x z=%08x\r\n", v0, v1, v2, v3);
    {
        u32 r0, r1, r2;
        Xil_Out32(b + R_CR, 0x4u);          /* soft reset, then watch bit2 */
        r0 = Xil_In32(b + R_CR);
        r1 = Xil_In32(b + R_CR);
        for (k = 0; k < 2000; k++) { }
        r2 = Xil_In32(b + R_CR);
        xil_printf("PRB0b rst=%08x %08x %08x\r\n", r0, r1, r2);
    }

    Xil_Out32(b + R_SR, 0x1770u);
    Xil_Out32(b + R_CR, 0x1u);
    Xil_Out32(b + R_ADDR, (u32)(UINTPTR)pbuf[0]);
    Xil_Out32(b + R_LEN, 4u);
    v4 = Xil_In32(b + R_SR);
    for (k = 0; k < 4000; k++) { }
    v5 = Xil_In32(b + R_SR);
    usleep(20000);
    v6 = Xil_In32(b + R_SR);
    v7 = Xil_In32(b + R_ADDR);
    xil_printf("PRB1 sr0=%08x sr1=%08x sr2=%08x sa=%08x\r\n", v4, v5, v6, v7);

    Xil_Out32(b + R_SR, 0x1770u);
    Xil_Out32(b + R_CR, 0x1u);
    Xil_Out32(b + R_ADDR, (u32)(UINTPTR)pbuf[0]);
    Xil_Out32(b + R_LEN, PB_WORDS * 4u);
    g_prb[0] = Xil_In32(b + R_CR);
    g_prb[1] = Xil_In32(b + R_SR);
    usleep(5000);   g_prb[2] = Xil_In32(b + R_SR);
    usleep(20000);  g_prb[3] = Xil_In32(b + R_SR);
    usleep(100000); g_prb[4] = Xil_In32(b + R_SR);
    usleep(200000); g_prb[5] = Xil_In32(b + R_SR);
    usleep(400000); g_prb[6] = Xil_In32(b + R_SR);
    g_prb[7] = Xil_In32(b + R_CR);
    xil_printf("PRB2 cr=%08x sr=%08x %08x %08x %08x %08x %08x cr2=%08x\r\n",
               g_prb[0], g_prb[1], g_prb[2], g_prb[3], g_prb[4], g_prb[5], g_prb[6], g_prb[7]);

    xil_printf("PRB3 ethcr=%08x ethsr=%08x audrxcr=%08x audrxsr=%08x\r\n",
               Xil_In32(DMA_ETH_BASE + DMA_TX + R_CR), Xil_In32(DMA_ETH_BASE + DMA_TX + R_SR),
               Xil_In32(DMA_AUD_BASE + DMA_RX + R_CR), Xil_In32(DMA_AUD_BASE + DMA_RX + R_SR));
}

/* Copy one 240-word chunk of a received packet into the write buffer.
   Returns 1 when the write buffer became full.

   NOTE (root cause of the 10 Hz "de" knocking, fixed 2026-10-26):
   the packet's own `chunk` field is 0/1 *inside one 10 ms frame*, not the
   index of a 5 ms slot inside the 100 ms buffer (20 of those).  Using
   `chunk * CHUNK_WORDS` as the buffer offset made every frame of the stream
   overwrite words 0..239 and 240..479 of the same buffer, while words
   480..4799 were never written at all and stayed zero.  pb_fill still
   accumulated 240 per packet, so after 20 packets the buffer was declared
   full and played: 10 ms of the newest frame followed by 90 ms of digital
   silence, once per buffer = once per 100 ms.  That is exactly what the
   microphone recorded (tone for ~20 ms, -56 dB for the rest, repeating every
   100.0 ms) and why the 880 Hz pitch was right while speech was unreadable.
   The buffer offset must be sequential, so it is taken from pb_fill. */
static int append_chunk(const u8 *pcm, u32 chunk)
{
    u32 off = (u32)pb_fill[pb_wr];
    u32 i;

    if (chunk >= 2u) return 0;
    if (off + CHUNK_WORDS > PB_WORDS) return 0;     /* stray packet: ignore */
    for (i = 0; i < CHUNK_WORDS; i++) {
        u32 w;
        memcpy(&w, pcm + i * 4u, 4);
        pbuf[pb_wr][off + i] = w;
    }
    pb_fill[pb_wr] += CHUNK_WORDS;
    return (pb_fill[pb_wr] >= PB_WORDS);
}

int main(void)
{
    u32 *res = (u32 *)RES_BASE;
    u32 n_mic = 0, n_mic_err = 0, n_dac = 0, n_under = 0, n_pkt = 0, rms = 0;
    u32 n_dac_rst = 0, n_rx_rst = 0, n_rx_any = 0, n_rx_err = 0;
    u32 n_mic_wd = 0, g_t_mic_ioc = 0;      /* MIC stall watchdog (see loop)  */
    u32 g_stat_last = 0;                    /* n_mic at the last res[] update */
    u32 link = 0, wait_ms = 0, phase = 0, i, len;
    int mic_cur = 0, rxp_cur = 0, pb_rd = 0, dac_off = 0, tone_mode = 1;
    int mic_armed = 0, rx_armed = 0, dac_armed = 0, net_started = 0;

    Xil_DCacheDisable();
    memset(res, 0, 128 * 4);
    res[0] = 0x4C4E4B31u;                      /* 'LNK1' */

    xil_printf("\r\n=== UDP audio link (eth_audio) ===\r\n");

    mdio_init();
    xil_printf("p4 id=%04x:%04x bmcr=%04x bmsr=%04x\r\n",
               mdio_rd(4, 2), mdio_rd(4, 3), mdio_rd(4, 0), mdio_rd(4, 1));
    mdio_wr(4, 9, 0x0000);
    mdio_wr(4, 4, 0x0101);
    mdio_wr(4, 0, 0x1300);
    while (wait_ms < 3000u) {
        u32 bs = mdio_rd(4, 1), bs2 = mdio_rd(4, 1);
        (void)bs;
        if (bs2 & 0x0004u) { link = 1; break; }
        usleep(50000); wait_ms += 50u;
    }
    xil_printf("p4 AN100 bmcr=%04x bmsr=%04x link=%u wait=%u ms\r\n",
               mdio_rd(4, 0), mdio_rd(4, 1), link, wait_ms);
    {
        u32 e = Xil_In32(ETH_BASE + 0x410);
        e = (e & ~0xC0000000u) | 0x40000000u;
        Xil_Out32(ETH_BASE + 0x410, e);
        xil_printf("EMMC=%08x\r\n", Xil_In32(ETH_BASE + 0x410));
    }

    dma_probe();

    /* ---- on-device denoise: create the RNNoise state once.  It is ~85 KB of
       model weights in .rodata plus a small heap allocation, so it is created
       here and never freed.  Default is BYPASS; send '1' on the console to
       switch to RNNoise, '0' to go back. ---- */
    g_st = rnnoise_create(0);
    xil_printf("rnnoise %s frame=%d\r\n", g_st ? "ready" : "FAILED",
               (int)rnnoise_get_frame_size());
    /* FPU: CPACR cp10/cp11 must be enabled or every VFP instruction traps into
       the undefined-instruction handler and gets emulated in software, which is
       what turned a 2.6 ms RNNoise frame into 20 ms.  boot.S only touches
       FPEXC.EN, so do the CPACR part here explicitly. */
    xil_printf("cpacr_before=%08x\r\n", *(volatile u32 *)0xE000ED88u);
    *(volatile u32 *)0xE000ED88u |= 0x00F00000u;
    __asm__ volatile ("dsb" ::: "memory");
    __asm__ volatile ("isb" ::: "memory");
    xil_printf("cpacr_after=%08x\r\n", *(volatile u32 *)0xE000ED88u);
    {
        u32 fa, fb;
        float acc = 1.0f;
        int q;
        fa = Xil_In32(TIMER_LOW);
        for (q = 0; q < 10000; q++) {
            acc = acc * 1.000001f + 0.5f;
        }
        fb = Xil_In32(TIMER_LOW);
        xil_printf("fmul10k=%u tk acc=%u\r\n", fb - fa, (u32)(acc > 0.0f));
    }
    {
        u32 ra, rb;
        int q;
        for (q = 0; q < (int)FRAME_WORDS; q++) {
            g_fin[q] = 0.0f;
            g_fout[q] = 0.0f;
        }
        ra = Xil_In32(TIMER_LOW);
        for (q = 0; q < 10; q++) {
            rnnoise_process_frame(g_st, g_fout, g_fin);
        }
        rb = Xil_In32(TIMER_LOW);
        xil_printf("rn_cost=%u tk/frame\r\n", (rb - ra) / 10u);
    }
    g_dsp_mode = 0;
    /* Power-on mode is bypass, so the PL denoiser must be told to stay out of
       the way.  This has to be an explicit write: the AXI GPIO output register
       resets to 0, and 0 means "dn_axis_0/bypass = 0" = spectral subtraction
       ON.  Without this line the board silently denoises while the ARM still
       reports mode 0, and every A/B comparison between "bypass" and "PL" then
       measures the same thing (found the hard way on 2026-11-02). */
    Xil_Out32(GPIO_BASE, 1u);
    /* 2026-11-02 包6：双 DAC 输出模式上电默认 M0（两口同源）。
       理由与上面一行相同 —— GPIO 输出寄存器复位值是 0，而 0 恰好就是 M0，
       但仍然显式写一次，避免以后改了 M 编码又踩同样的坑。 */
    g_dac_mode = 0u;
    hw_gpio2_probe();           /* 先确认通道2 真是我们的输出寄存器（写 1 读 1） */
    hw_gpio2_apply();           /* TRI=0 + M0 + 底噪位 0（打包写，一处入口） */
    g_dly_frames = 0u;          /* start with no added latency in bypass    */
    gate_flush_reset();
    /* Time scaling starts OFF (rate 100 = identity).  Power-on behaviour must
       be byte-for-byte what it was before this feature existed -- that is the
       whole meaning of "default".  Explicitly zeroed rather than relying on
       BSS, so a warm restart through this path resets it too. */
    ts_reset(&g_ts);
    g_ts_rate    = TS_RATE_DEF;
    g_ts_apply   = 0u;
    g_ts_rejects = 0u;

    /* ---- DAC: start in tone mode, the feeder synthesises the 440 Hz tone on
       demand, so nothing has to be pre-filled here ---- */
    for (i = 0; i < PB_N; i++) {
        pb_ready[i] = 0;
        pb_fill[i]  = 0;
    }

    /* One single soft reset of the audio DMA, before anything is running.
       Both channels of axi_dma_a are reset by it, so the MIC channel is armed
       first and the DAC channel last -- after this point nothing resets the IP
       again (see dma_start_nr). */
    {
        u32 guard = 0;
        Xil_Out32(DMA_AUD_BASE + DMA_TX + R_CR, CR_RESET);
        while ((Xil_In32(DMA_AUD_BASE + DMA_TX + R_CR) & CR_RESET) && guard++ < 1000000u) { }
    }

    Xil_DCacheInvalidateRange((UINTPTR)micbuf[mic_cur], AUDIO_BYTES);
    dma_start_nr(DMA_AUD_BASE, DMA_RX, (u32)(UINTPTR)micbuf[mic_cur], AUDIO_BYTES);
    mic_armed = 1;

    /* Same deal for the Ethernet DMA: reset both of its channels once, here,
       and never again -- send_udp() runs every 10 ms and used to soft reset the
       whole IP, which killed the receive channel (see dma_start_nr). */
    {
        u32 guard = 0;
        Xil_Out32(DMA_ETH_BASE + DMA_RX + R_CR, CR_RESET);
        while ((Xil_In32(DMA_ETH_BASE + DMA_RX + R_CR) & CR_RESET) && guard++ < 1000000u) { }
    }

    Xil_DCacheInvalidateRange((UINTPTR)rxpkt[rxp_cur], RXP_BYTES);
    dma_start_nr(DMA_ETH_BASE, DMA_RX, (u32)(UINTPTR)rxpkt[rxp_cur], RXP_BYTES);
    rx_armed = 1;

    arm_dac(pb_rd, dac_off, (int)PB_CHUNK_WORDS);
    dac_armed = 1;
    xil_printf("DAC tone 440 Hz started (network stream replaces it)\r\n");

    for (;;) {
        int r;

        /* ---------- status uplink: the board's own truth, 4x per second ----
           The period is derived from the MEASURED 10 ms MIC frame interval, not
           from a frame counter: in mode 1 the uplink frame rate drops to ~49 fps
           and a frame-counted tick would silently stretch to 500 ms. */
        {
            static u32 st_t = 0;
            static u32 st_v = 0;
            u32 now = Xil_In32(TIMER_LOW);
            u32 period = (g_frame_dt ? g_frame_dt : 100000u) * 25u;
            if (!st_v) { st_t = now; st_v = 1u; }
            else if ((u32)(now - st_t) >= period) { st_t = now; send_status(); }
        }

        /* ---------- DAC: self-heal if the channel was reset behind our back --
           The AXI DMA soft reset is IP-global (see dma_start_nr), so any code
           path that resets the MIC channel would also drop the DAC channel to
           DMASR=0x1 / DMACR.RS=0 with no IOC and no error.  Detect exactly that
           state and let the feeder below re-arm the current chunk. */
        if (dac_armed && dma_is_reset(DMA_AUD_BASE, DMA_TX)) {
            n_dac_rst++;
            dac_armed = 0;
        }

        /* ---------- DAC: one 25 ms chunk finished ---------- */
        if (dac_armed) {
            r = dma_poll(DMA_AUD_BASE, DMA_TX);
            if (r != 0) {
                u32 tn = Xil_In32(TIMER_LOW);
                dac_armed = 0;
                if (r > 0) {
                    u32 dt = tn - g_t_arm;
                    if (!g_dac_dt) g_dac_dt = dt;
                    if (g_chk_n == 0u || dt < g_chk_min) g_chk_min = dt;
                    if (dt > g_chk_max) g_chk_max = dt;
                    g_chk_sum += dt; g_chk_n++;
                    res[88u + g_cri] = (dt & 0x00FFFFFFu) |
                                       (tone_mode ? 0u : 0x01000000u);
                    g_cri = (g_cri + 1u) % CHK_RING;
                    g_t_ioc = tn;
                    g_t_ioc_v = 1u;
                    dac_off += (int)PB_CHUNK_WORDS;
                    if (dac_off >= (int)PB_WORDS) {     /* whole buffer played */
                        /* A 4800-word buffer IS 100 ms of 48 kHz audio, so this
                           period in ticks calibrates the timer without any
                           external reference: ticks_per_10ms = bd / 10, and a
                           bd above 100 ms means the DAC sat on empty FIFO. */
                        u32 bd = tn - g_t_pbnd;
                        if (g_buf_v) {
                            if (g_buf_n == 0u || bd < g_buf_min) g_buf_min = bd;
                            g_buf_n++;
                        }
                        g_t_pbnd = tn;
                        g_buf_v  = 1u;
                        dac_off = 0;
                        n_dac++;
                        pb_ready[pb_rd] = 0;
                        pb_fill[pb_rd]  = 0;
                    }
                } else {
                    n_under++;
                }
            }
        }
        /* ---------- DAC: keep the MM2S channel fed, one chunk at a time ------
           tone_mode is the "nothing to stream" state: the same buffer is
           refilled with the 440 Hz self-test tone and replayed, so the speaker
           is never silent and never gaps.  A complete network buffer switches
           it back to real audio.  The silence test uses the global timer, not
           a loop count (a loop count is not time). */
        if (!dac_armed) {
            if (dac_off == 0) {
                int nxt = (pb_rd + 1) & (int)(PB_N - 1);
                if (pb_ready[nxt]) {                    /* real data is ready */
                    int lead = 0, k;
                    /* how many buffers are queued ahead at the moment we
                       commit to the network stream?  1 == zero margin. */
                    for (k = 0; k < (int)PB_N; k++) {
                        if (pb_ready[(nxt + k) & (int)(PB_N - 1)]) lead++;
                        else break;
                    }
                    if (lead >= 2) g_lead2++;
                    if (tone_mode) { g_switch++; g_tone_out++; }
                    pb_rd     = nxt;
                    tone_mode = 0;
                    g_sil_t   = 0;
                } else if (pb_ready[pb_rd]) {
                    g_sil_t = 0;
                } else {
                    u32 now = Xil_In32(TIMER_LOW);
                    if (tone_mode) {
                        g_sil_t = now;
                    } else if (g_sil_t == 0) {
                        g_sil_t = now;                  /* silence starts now */
                    } else if ((now - g_sil_t) > STARVE_TICKS) {
                        tone_mode = 1;                  /* long silence -> tone */
                        g_tone_in++;
                        n_under++;
                    }
                }
                if (tone_mode && !pb_ready[pb_rd]) {
                    if (g_tone_en) {
                        fill_tone(pbuf[pb_rd], PB_WORDS, &phase);
                    } else {
                        memset(pbuf[pb_rd], 0, PB_WORDS * 4u);
                    }
                    apply_gain(pbuf[pb_rd], PB_WORDS);
                    pb_ready[pb_rd] = 1;
                    pb_fill[pb_rd]  = PB_WORDS;
                }
            }
            if (dac_off > 0 || pb_ready[pb_rd]) {
                int rem = (int)PB_WORDS - dac_off;
                int n   = (rem > (int)PB_CHUNK_WORDS) ? (int)PB_CHUNK_WORDS : rem;
                if (dac_off == 0) {
                    /* 100 ms buffer boundary: for how long was the MM2S left
                       with nothing queued?  The PL FIFO (512 words = 10.7 ms)
                       hides the first 10.7 ms of it; anything beyond is
                       audible silence, and it never reaches n_under because
                       the >30 ms tone fallback does not trigger. */
                    u32 now2 = Xil_In32(TIMER_LOW);
                    u32 bnd  = g_t_ioc_v ? (now2 - g_t_ioc) : 0u;
                    u32 gt   = (g_frame_dt && bnd > (g_frame_dt >> 1)) ? 1u : 0u;
                    res[24u + g_bri] = (bnd & 0x00FFFFFFu)
                                     | (tone_mode ? 0u : 0x01000000u)
                                     | (gt ? 0x02000000u : 0u);
                    res[62] = now2;              /* so the delta can be checked */
                    res[63] = g_t_ioc;           /* by hand from the dump      */
                    g_bri = (g_bri + 1u) % BND_RING;
                    if (!tone_mode) {
                        g_bnd_sum += bnd;
                        g_bnd_n++;
                        if (bnd > g_bnd_max) g_bnd_max = bnd;
                        if (bnd > g_bnd_win_max) g_bnd_win_max = bnd;
                        if (gt) g_bnd_gt++;
                    }
                }
                arm_dac(pb_rd, dac_off, n);
                dac_armed = 1;
            }
        }

        /* ---------- Ethernet receive ---------- */
        if (rx_armed && dma_is_reset(DMA_ETH_BASE, DMA_RX)) {
            n_rx_rst++;                     /* reset behind our back: re-arm */
            rx_armed = 0;
        }
        if (rx_armed) {
            r = dma_poll(DMA_ETH_BASE, DMA_RX);
            if (r != 0) {
                int prev = rxp_cur;

                /* Length must be read before re-arming (the new transfer
                   overwrites the register), and the re-arm itself must happen
                   before any packet parsing: the MAC has no place to put a
                   frame while the channel is unarmed. */
                if (r > 0) len = Xil_In32(DMA_ETH_BASE + DMA_RX + R_LEN);
                else       n_rx_err++;
                rxp_cur = (rxp_cur + 1) & (int)(RXP_N - 1);
                Xil_DCacheInvalidateRange((UINTPTR)rxpkt[rxp_cur], RXP_BYTES);
                dma_start_nr(DMA_ETH_BASE, DMA_RX, (u32)(UINTPTR)rxpkt[rxp_cur], RXP_BYTES);
                rx_armed = 1;
                if (r > 0) {
                    n_rx_any++;             /* any frame at all, before filtering */
                    if (len >= 42u && rxpkt[prev][12] == 0x08 && rxpkt[prev][13] == 0x00) {
                        const u8 *ip = &rxpkt[prev][14];
                        u32 ihl = (ip[0] & 0x0Fu) * 4u;
                        if (ip[9] == 17u) {
                            const u8 *u = ip + ihl;
                            u32 dport = ((u32)u[2] << 8) | u[3];
                            const u8 *pay = u + 8u;
                            if (dport == PORT_IN && pay[0] == 'A' && pay[1] == 'U') {
                                u32 chunk = ((u32)pay[4] << 8) | pay[5];
                                u32 tpn = Xil_In32(TIMER_LOW);
                                n_pkt++;
                                /* packet arrival pacing, as seen by THIS board
                                   (the PC side may look clean and still burst
                                   through the RX ring).  Nominal spacing is
                                   5 ms = dtm/2, so >8 ms / >15 ms are stalls. */
                                if (g_t_pkt_v) {
                                    u32 pg = tpn - g_t_pkt;
                                    g_pgap_sum += pg;
                                    if (pg > g_pgap_max) g_pgap_max = pg;
                                    if (g_frame_dt && pg > (g_frame_dt * 4u) / 5u) g_pgap8++;
                                    if (g_frame_dt && pg > (g_frame_dt * 3u) / 2u) g_pgap15++;
                                }
                                g_t_pkt = tpn; g_t_pkt_v = 1;
                                if (!net_started) {
                                    net_started = 1;
                                    pb_wr = (pb_rd + 1) & (int)(PB_N - 1);
                                    pb_fill[pb_wr] = 0;
                                    pb_ready[pb_wr] = 0;
                                    xil_printf("net stream detected\r\n");
                                }
                                /* would this write land in the buffer the DMA is
                                   playing right now?  If this ever counts, the
                                   ring is too shallow and chunks get clobbered. */
                                if (pb_wr == pb_rd) g_app_over++;
                                if (append_chunk(pay + 8, chunk)) {
                                    apply_gain(pbuf[pb_wr], PB_WORDS);
                                    pb_ready[pb_wr] = 1;
                                    pb_wr = (pb_wr + 1) & (int)(PB_N - 1);
                                    pb_fill[pb_wr] = 0;
                                    pb_ready[pb_wr] = 0;
                                }
                            } else if (dport == PORT_CTL &&
                                       ((((u32)u[4] << 8) | u[5]) > 8u)) {
                                /* control plane: the PL UART RX path proved
                                   unreliable, so mode changes also arrive as a
                                   one-byte UDP datagram. PCB-friendly and it
                                   keeps the console free for logs. */
                                u8 c = pay[0];
                                if (c >= '0' && c <= '3') {
                                    /* same code path as the console -- this
                                       used to be a second, incomplete copy */
                                    set_dsp_mode((u32)(c - '0'));
                                } else if (c == 'S') {
                                    /* ---- time-scaling ratio, two-byte payload
                                       The single-byte space is full ('0'..'2',
                                       'a'..'h', 'i','j','k','K','n','N','t','T'),
                                       and a raw percentage byte would collide
                                       with ASCII digits.  So 'S' introduces a
                                       second byte holding the percentage, which
                                       keeps the existing one-byte commands
                                       untouched and makes the new one
                                       unambiguously longer.
                                      'S' is IDEMPOTENT like every other control
                                       command: sending the same percentage twice
                                       is a no-op (ts_parse_rate returns early),
                                       which is what makes it safe over a
                                       broadcast that arrives 2-3 times.
                                       Out-of-range values are REJECTED with a
                                       printed reason, never clamped -- see
                                       ts_parse_rate(). */
                                    /* Chunk length from the UDP header:
                                       u[4..5] is the UDP datagram length,
                                       which includes the 8-byte UDP header.
                                       So payload bytes = len - 8, and the
                                       ratio byte exists iff that is >= 2.
                                       (Do NOT use the packet's own `chunk`
                                       field: that is the 0/1 sub-frame index
                                       of the DAC stream, unrelated here.) */
                                    if ((((u32)u[4] << 8) | u[5]) >= 10u) {
                                        ts_parse_rate((u32)pay[1]);
                                    } else {
                                        xil_printf("ts: 'S' needs 2 bytes, "
                                                   "got 1\r\n");
                                    }
                                } else if (c == 't') {
                                    /* NOT a toggle any more: the datagram is a
                                       broadcast and arrives 2-3 times, so an
                                       XOR turned the tone on and straight back
                                       off.  't' = on, 'T' = off.  A repeated 't'
                                       is now a no-op instead of a flicker. */
                                    g_tone_en = 1u;
                                    xil_printf("tone=1\r\n");
                                } else if (c == 'T') {
                                    g_tone_en = 0u;
                                    xil_printf("tone=0\r\n");
                                } else if (c == 'i' || c == 'j') {
                                    /* latency ladder.  These were missing here
                                       entirely -- the console sent them and the
                                       board ignored them, which is why "the
                                       latency setting did nothing". */
                                    num_set_latency(c == 'i' ? 30u : 20u);
                                } else if (c == 'k' || c == 'K') {
                                    /* independent delay on/off.  Kept apart
                                       from the ladder above on purpose: the
                                       ladder says "how much", this says
                                       "whether at all", and turning the
                                       delay off must not forget the depth
                                       the operator dialled in.  Broadcast
                                       duplicates are absorbed inside
                                       num_set_lat_bypass (same value = no
                                       -op), so this is safe to repeat. */
                                    num_set_lat_bypass(c == 'K' ? 1u : 0u);
                                } else if (c == 'a' || c == 'b' ||
                                           c == 'c' || c == 'd') {
                                    /* explicit noise levels instead of a cycle:
                                       the control datagram is a broadcast, so it
                                       arrives 2-3 times and a toggle would step
                                       several levels at once.  Levels chosen so
                                       the demo stays audible without drowning the
                                       talker (1500 makes speech unintelligible). */
                                    g_noise = (c == 'a') ? 0u   :
                                              (c == 'b') ? 300u :
                                              (c == 'c') ? 800u : 1500u;
                                    g_nz_l = 0;
                                    g_nz_r = 0;
                                    /* 方案A：档位同时写进 PL（GPIO 通道2
                                       的 bit[3:2] -> dn_axis_0/nz），
                                       PS 侧只在 mode != 3 时才注入。 */
                                    hw_gpio2_apply();
                                    xil_printf("noise=%u\r\n", g_noise);
                                } else if (c >= 'e' && c <= 'h') {
                                    /* output gain, four *explicit* steps rather
                                       than a cycle: the control datagram is a
                                       broadcast and arrives 2-3 times, so a
                                       toggle would skip steps.  Q15 again:
                                       32768 = 0 dB, 24576 = -2.5 dB,
                                       16384 = -6 dB, 8192 = -12 dB. */
                                    g_gain = (c == 'e') ? 32768 :
                                             (c == 'f') ? 24576 :
                                             (c == 'g') ? 16384 : 8192;
                                    xil_printf("gain=%u%% q15=%d\r\n",
                                               (unsigned)((g_gain * 100) / 32768),
                                               g_gain);
                                } else if (c == 'n') {
                                    /* was a toggle -- same broadcast problem as
                                       't'.  'n' = 800, 'N' = 0. */
                                    g_noise = 800u;
                                    g_nz_l = 0;
                                    g_nz_r = 0;
                                    hw_gpio2_apply();   /* PL 噪声位同步 */
                                    xil_printf("noise=%u\r\n", g_noise);
                                } else if (c == 'N') {
                                    g_noise = 0u;
                                    g_nz_l = 0;
                                    g_nz_r = 0;
                                    hw_gpio2_apply();   /* PL 噪声位同步 */
                                    xil_printf("noise=0\r\n");
                                } else if (c == 'm' || c == 'o' || c == 'p') {
                                    /* 包6：双 DAC 输出模式。
                                         'm' = M0 两口同源（默认，用来 A/B 两个 DAC 的音质）
                                         'o' = M1 板载放左、外接放右
                                         'p' = M2 板载静音、外接放完整立体声
                                       和别的控制字节一样是幂等的：值没变就不碰 GPIO，
                                       这样广播重发 2-3 次不会有副作用。 */
                                    u32 m = (c == 'm') ? 0u :
                                            (c == 'o') ? 1u : 2u;
                                    if (m != g_dac_mode) {
                                        g_dac_mode = m;
                                        hw_gpio2_apply();   /* mode+噪声位一起写 */
                                        xil_printf("dac_mode=%u\r\n", m);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        if (!rx_armed) {                    /* e.g. after the reset guard above */
            Xil_DCacheInvalidateRange((UINTPTR)rxpkt[rxp_cur], RXP_BYTES);
            dma_start_nr(DMA_ETH_BASE, DMA_RX, (u32)(UINTPTR)rxpkt[rxp_cur], RXP_BYTES);
            rx_armed = 1;
        }

        /* ---------- MIC: stall watchdog ----------------------------------
           Seen once on 10/27: n_mic froze at 3500 (~35 s) with the CPU still
           alive inside this loop -- the S2MM channel simply stopped raising
           IOC, and there is no sticky error bit to poll for it.  So watch the
           global timer: 200 ms without a MIC frame means the channel is gone.
           Rebuild it the same way startup does -- and the DAC with it, because
           a DMA soft reset hits the whole IP (see dma_start_nr) and the MM2S
           channel would otherwise sit halted with no IOC.  Cost when healthy:
           one timer read per 10 ms frame. */
        if (mic_armed && g_t_mic_ioc != 0u) {
            u32 wnow = Xil_In32(TIMER_LOW);
            u32 wlim = g_frame_dt ? (g_frame_dt * 20u) : 7000000u;
            if ((u32)(wnow - g_t_mic_ioc) > wlim) {
                u32 late = wnow - g_t_mic_ioc;
                u32 guard = 0;
                int q;
                n_mic_wd++;
                mic_armed = 0;
                dac_armed = 0;
                Xil_Out32(DMA_AUD_BASE + DMA_TX + R_CR, CR_RESET);
                while ((Xil_In32(DMA_AUD_BASE + DMA_TX + R_CR) & CR_RESET) &&
                       guard++ < 1000000u) { }
                mic_cur = 0;
                Xil_DCacheInvalidateRange((UINTPTR)micbuf[0], AUDIO_BYTES);
                dma_start_nr(DMA_AUD_BASE, DMA_RX, (u32)(UINTPTR)micbuf[0], AUDIO_BYTES);
                mic_armed = 1;
                dac_off  = 0;
                pb_rd    = 0;
                tone_mode = 1;
                for (q = 0; q < (int)PB_N; q++) { pb_ready[q] = 0; pb_fill[q] = 0; }
                g_t_mic_ioc = wnow;
                xil_printf("mic watchdog: audio DMA re-armed after %u ticks\r\n", late);
            }
        }

        /* ---------- MIC: a 10 ms frame finished ---------- */
        if (mic_armed) {
            r = dma_poll(DMA_AUD_BASE, DMA_RX);
            if (r != 0) {
                u32 *mb = micbuf[mic_cur];
                int k;

                mic_armed = 0;
                if (r < 0) n_mic_err++;
                /* re-arm the other buffer first so no beat is lost */
                mic_cur ^= 1;
                Xil_DCacheInvalidateRange((UINTPTR)micbuf[mic_cur], AUDIO_BYTES);
                dma_start_nr(DMA_AUD_BASE, DMA_RX, (u32)(UINTPTR)micbuf[mic_cur], AUDIO_BYTES);
                mic_armed = 1;

                if (r > 0) {
                    u32 tnow = Xil_In32(TIMER_LOW);
                    /* frame 0->1 is a startup artifact (the acc_rx FIFO is
                       already primed when the first transfer is armed), so the
                       real MIC interval is taken later, in steady state. */
                    if (n_mic == 20u || n_mic == 200u) {
                        g_fd_ref = tnow;
                    } else if (n_mic == 21u) {
                        g_frame_dt = tnow - g_fd_ref;
                    } else if (n_mic == 201u) {
                        g_frame_dt2 = tnow - g_fd_ref;
                    }
                    g_last_t = tnow;
                    g_t_mic_ioc = tnow;      /* feeds the stall watchdog */
                    mic_mode_poll();
                    if (g_dsp_mode == 1u) {
                        /* Latency-for-quality mode: stage 1 only.  The frame
                           goes into the raw ring; whether it can be sent
                           RIGHT NOW no longer decides whether it survives. */
                        rn_push_in(mb);
                        /* Interim continuity fix, see g_rn_pass above.  Odd
                           frames are transmitted raw so the PC never has to
                           conceal long stretches of silence. */
                        if (g_rn_pass) {
                            g_rn_alt ^= 1u;
                            if (g_rn_alt) {
                                memcpy(pl, (u8 *)mb, CHUNK_BYTES);
                                send_udp(pl, CHUNK_BYTES, n_mic, 0, PORT_MIC);
                                memcpy(pl, (u8 *)mb + CHUNK_BYTES, CHUNK_BYTES);
                                send_udp(pl, CHUNK_BYTES, n_mic, 1, PORT_MIC);
                                n_mic++;
                                g_pass_n++;
                            }
                        }
                    } else {
                        /* Modes 0 and 2.  mic_process may return NULL while the
                           delay line is still filling -- then this frame simply
                           is not sent yet, exactly like the batch path. */
                        u32 *out = mic_process(mb);
                        if (out) {
                            rms = 0;
                            for (k = 0; k < (int)FRAME_WORDS; k++) {
                                s32 l = (s16)(out[k] & 0xFFFFu);
                                rms += (u32)((l * l) >> 12);
                            }
                            rms /= FRAME_WORDS;

                            memcpy(pl, (u8 *)out, CHUNK_BYTES);
                            send_udp(pl, CHUNK_BYTES, n_mic, 0, PORT_MIC);
                            memcpy(pl, (u8 *)out + CHUNK_BYTES, CHUNK_BYTES);
                            send_udp(pl, CHUNK_BYTES, n_mic, 1, PORT_MIC);
                            n_mic++;
                        }
                    }
                }
            }
        }

        /* ---------- MIC mode 1: stages 2 and 3 ------------------------------
           Every pass, independently of the DMA poll, in this order:
             1. denoise ONE frame if the raw ring has any and the out ring has room
             2. send AT MOST ONE finished frame
           Both are one-per-pass on purpose: each rn_frame() costs 20 ms, so
           anything that runs a whole batch (or drains the whole out ring) in
           one pass blocks the transmitter for hundreds of milliseconds and the
           uplink falls to ~31 fps (measured; see the note on rn_batch_once).

           THE BACKLOG TARGET.  Input arrives at 100 fps, the CPU denoises
           ~46 fps, so the raw ring would grow without bound unless something
           throttles the output.  That something is `g_rn_target`: stage 3 only
           sends while the combined backlog (raw + finished) is ABOVE target.
           When the backlog reaches target, sending stops until input pushes it
           back over -- at which point one frame goes out and the backlog
           returns to target.  In steady state the backlog is pinned at
           g_rn_target frames and the output rate equals the input rate again,
           with the CPU draining a fixed cushion.  That cushion IS the latency
           the operator chose.

           DELAY OFF.  When g_lat_off is set the throttle is removed: a frame
           goes out the instant it is denoised.  That zeroes the *added*
           latency, but it cannot make RNNoise real-time -- 20.07 ms per frame
           still caps throughput at ~49 fps, so the backlog will now grow
           without bound and fill the ring.  rn_batch_once() therefore still
           refuses to write into a full out-ring, and rn_push_in() drops on a
           full in-ring; g_qdrops counts it.  In other words "delay off" for
           mode 1 degrades to the drop-frames behaviour, which is the honest
           consequence of the cost model, not a bug. */
        if (g_dsp_mode == 1u) {
            rn_batch_once();
            if (g_lat_off ? (g_out_fill > 0u)
                          : ((g_in_fill + g_out_fill) > g_rn_target)) {
                u32 *fb = rn_pop_out();
                if (fb) {
                    u32 *tx = fb;       /* what will actually be queued     */
                    int kk;

                    /* ---- time scaling (rate 50..100) -------------------
                       Inserted AFTER denoising and BEFORE the UDP framing,
                       for two reasons:
                         a) the denoised CONTENT must not be altered -- the
                            whole point of mode B is that nothing is lost,
                            only delivered slower;
                         b) the UDP sequence number (n_mic) has to advance
                            once per transmitted frame, so the scaler must
                            be inside that cadence, not beside it.

                       The scaler always returns exactly FRAME_WORDS points:
                       it absorbs the "extra" samples of a stretched frame
                       into its own queue and hands them back over the next
                       frames.  That is what keeps the uplink at 100 fps
                       whatever the ratio -- the PC would otherwise see the
                       frame counter stall and conclude the board had died.

                       Cost measured on the A9 for the worst case (rate=50,
                       L=960): a 960-point float copy plus a 480-point
                       weighted sum, i.e. tens of microseconds -- three
                       orders of magnitude below the 20.07 ms rn_frame() it
                       sits behind, so it cannot become the new bottleneck. */
                    if (g_ts_apply) {
                        static float ts_in[FRAME_WORDS];
                        static float ts_out[FRAME_WORDS];
                        static u32   ts_dst[FRAME_WORDS];
                        int i;

                        for (i = 0; i < (int)FRAME_WORDS; i++) {
                            float l = (float)(s32)(s16)(fb[i] & 0xFFFFu);
                            float r = (float)(s32)(s16)((fb[i] >> 16) & 0xFFFFu);
                            ts_in[i] = 0.5f * (l + r);   /* mono, as RNNoise */
                        }
                        ts_frame(&g_ts, ts_in, ts_out, (int)g_ts_rate);
                        for (i = 0; i < (int)FRAME_WORDS; i++) {
                            s32 v = (s32)ts_out[i];
                            if (v > 32767)  v = 32767;
                            if (v < -32768) v = -32768;
                            ts_dst[i] = ((u32)((u16)v) << 16) | ((u16)v & 0xFFFFu);
                        }
                        tx = ts_dst;
                    }

                    rms = 0;
                    for (kk = 0; kk < (int)FRAME_WORDS; kk++) {
                        s32 l = (s16)(tx[kk] & 0xFFFFu);
                        rms += (u32)((l * l) >> 12);
                    }
                    rms /= FRAME_WORDS;

                    memcpy(pl, (u8 *)tx, CHUNK_BYTES);
                    send_udp(pl, CHUNK_BYTES, n_mic, 0, PORT_MIC);
                    memcpy(pl, (u8 *)tx + CHUNK_BYTES, CHUNK_BYTES);
                    send_udp(pl, CHUNK_BYTES, n_mic, 1, PORT_MIC);
                    n_mic++;
                }
            }
        }

        /* one-shot dump of the TEMAC statistics block: distinguishes "the MAC
           dropped it" (overrun counter climbs) from "the PC never sent it" */
        if (n_mic == 1000u) {
            xil_printf("mac=%08x,%08x,%08x,%08x,%08x,%08x,%08x,%08x\r\n",
                       Xil_In32(ETH_BASE + 0x200u), Xil_In32(ETH_BASE + 0x204u),
                       Xil_In32(ETH_BASE + 0x208u), Xil_In32(ETH_BASE + 0x20Cu),
                       Xil_In32(ETH_BASE + 0x210u), Xil_In32(ETH_BASE + 0x214u),
                       Xil_In32(ETH_BASE + 0x218u), Xil_In32(ETH_BASE + 0x21Cu));
        }

        /* Statistics.  Deliberately silent while a network stream is running:
           the old ~330-char line takes ~28 ms at 115200 baud, far longer than
           the 10.7 ms PL FIFO, so the print itself manufactured the very
           dropouts we are chasing.  Everything goes to res[] in DDR instead
           (xsdb dumps all 128 words in one go, after the run).

           BUG FIXED 2026-10-29: the old guard was `n_mic % 500 == 0`, which
           in mode 1 never matched -- n_mic advances in steps of 30 there, and
           30 * k is never a multiple of 500.  res[] therefore froze at
           whatever it last held and every counter looked bogus over JTAG.
           The window is now "500 frames since the last update", which works
           for any step size. */
        if (n_mic != 0u && (n_mic - g_stat_last) >= 500u) {
            g_stat_last = n_mic;
            res[4]  = n_pkt;     res[5] = n_mic_err;  res[6] = rms;
            res[7]  = (n_dac_rst & 0xFFu) | ((n_rx_rst & 0xFFu) << 8) |
                      ((n_rx_err & 0xFFu) << 16) | ((g_app_over & 0xFFu) << 24);
            res[8]  = g_bri;
            res[9]  = g_bnd_n;                       /* window: boundaries seen */
            res[10] = g_bnd_n ? (g_bnd_sum / g_bnd_n) : 0u;   /* avg unarmed ticks */
            res[11] = g_bnd_max;                     /* worst stall, global */
            res[12] = g_bnd_gt;                      /* boundaries > 5 ms, global */
            res[13] = (g_switch & 0xFFu) | ((g_lead2 & 0xFFu) << 8) |
                      ((g_tone_in & 0xFFu) << 16) | ((g_tone_out & 0xFFu) << 24);
            res[14] = g_cri;
            res[15] = g_frame_dt;                    /* 480 words: the tick/s */
            res[16] = g_dac_dt;                      /* first chunk arm->IOC */
            res[17] = g_chk_n;
            res[18] = g_chk_n ? (g_chk_sum / g_chk_n) : 0u;
            res[19] = g_chk_min;                     /* shortest chunk, global */
            res[20] = g_chk_max;
            res[21] = (g_pgap8 & 0xFFu) | ((g_pgap15 & 0xFFu) << 8) |
                      ((g_txstall & 0xFFu) << 16);
            res[22] = g_pgap_max;
            res[23] = n_rx_any;
            res[24] = g_rn_batches;                  /* RNNoise batches done */
            res[25] = g_qmax;                        /* deepest backlog seen */
            res[26] = g_in_fill;                     /* raw frames waiting   */
            res[27] = g_rn_target;                   /* frames per batch     */
            res[28] = g_out_fill;                    /* denoised, unsent     */
            res[29] = g_qdrops;                      /* frames lost to full  */
            res[30] = g_dly_frames;                  /* delay line depth     */
            res[31] = g_dly_fill;                    /* delay line occupancy */
            res[32] = g_rn_target;
            res[33] = g_in_fill;
            res[34] = g_out_fill;
            res[35] = g_qmax;
            res[36] = g_qdrops;
            res[37] = g_rn_batches;
            res[38] = g_lat_off;                     /* 1 = zero added delay */
            res[39] = g_dsp_mode;                    /* 0 bypass 1 rn 2 gate */
            res[40] = g_ts_rate;                     /* time scale 50..100   */
            res[41] = g_ts_apply;                    /* 1 = stretching now   */
            res[42] = g_ts_rejects;                  /* out-of-range requests*/
            res[43] = (u32)ts_out_len((int)g_ts_rate); /* points per frame   */
            res[44] = g_pass_n;                      /* frames sent raw     */
            res[45] = g_rn_pass;                     /* 1 = pass-through on */
            res[56] = g_buf_min;                     /* 100 ms buffer, in ticks */
            res[57] = g_buf_n;
            res[58] = Xil_In32(TIMER_LOW);           /* absolute tick snapshot, */
            res[59] = Xil_In32(TIMER_LOW + 4u);      /* 5 s apart: the timer    */
            res[60] = g_frame_dt2;                   /* frequency, measured     */
            res[61] = g_bnd_win_max;
            Xil_DCacheFlushRange((UINTPTR)res, 128 * 4);
            if (n_pkt == g_pkt_seen) {               /* idle: printing is free */
                xil_printf("mic=%u dac=%u under=%u pkt=%u rms=%u dtm=%08x dtd=%08x "
                           "buf=%u/%u dt2=%08x "
                           "bnd=%u/%u/%u chk=%u/%u/%u sw=%u lead=%u tin=%u tout=%u "
                           "pg=%u/%u/%u txst=%u clob=%u dsp=%u bp=%u dm=%u gp=%u nz=%u tk=%u gt=%u/%u/%u g2=%u pr=%u\r\n",
                           n_mic, n_dac, n_under, n_pkt, rms, g_frame_dt, g_dac_dt,
                           res[56], res[57], res[60],
                           res[10], res[11], res[12],
                           res[18], res[19], res[20],
                           g_switch, g_lead2, g_tone_in, g_tone_out,
                           g_pgap8, g_pgap15, g_pgap_max, g_txstall, g_app_over,
                           g_dsp_mode, (Xil_In32(GPIO_BASE) & 1u), g_dac_mode,
                           g_gpio2_rb, g_noise, g_dsp_ticks,
                           (u32)g_gate_prime, (u32)g_gate_nf,
                           (u32)(g_gate_g * 1000.0f),
                           /* g2 = GPIO 通道2 原始 4 位读回（方案A 打包写）：
                              bit[1:0]=DAC 模式, bit[3:2]=PL 底噪档，用来在
                              串口上直接确认"档位真的进了寄存器"。 */
                           (Xil_In32(GPIO_MODE_BASE) & 0xFu),
                           g_gpio2_present);
            }
            g_pkt_seen = n_pkt;
            g_chk_sum = 0; g_chk_n = 0;              /* per-window averages */
            g_bnd_sum = 0; g_bnd_n = 0; g_bnd_win_max = 0;
        }
    }
    return 0;
}
