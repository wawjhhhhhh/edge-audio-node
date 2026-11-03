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
static int pb_wr;                 /* buffer the network fills */
static u32 g_arm_cr, g_arm_sr, g_arm_sr2;

/* MEASURED 2026-10-28: this table used to be asymmetric -- the positive peak
   was 8371 but the negative peak only -5169 (-38%), the zero crossings sat at
   index 0 and 33.5 instead of 0 and 32, and the last entry (2169) jumped
   straight to the first (0).  That is not a sine: it is a 440 Hz wave with a
   dominant 2nd harmonic and THD+N ~ -9 dB, which is why the self-test tone
   sounds obviously different from the same 440 Hz pushed over the network
   (the network path carries a real 16-bit sine, error 0 LSB).  Regenerated as
   round(8371*sin(2*pi*i/64)), i = 0..63. */
static const s16 sine64[64] = {
       0,   821,  1633,  2430,  3203,  3946,  4651,  5311,
    5919,  6471,  6960,  7383,  7734,  8011,  8210,  8331,
    8371,  8331,  8210,  8011,  7734,  7383,  6960,  6471,
    5919,  5311,  4651,  3946,  3203,  2430,  1633,   821,
       0,  -821, -1633, -2430, -3203, -3946, -4651, -5311,
   -5919, -6471, -6960, -7383, -7734, -8011, -8210, -8331,
   -8371, -8331, -8210, -8011, -7734, -7383, -6960, -6471,
   -5919, -5311, -4651, -3946, -3203, -2430, -1633,  -821
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
static void send_udp(const u8 *payload, u32 n, u32 seq, u32 chunk, u32 port)
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

    udp[8]  = 'A'; udp[9] = 'U';
    udp[10] = (u8)(seq >> 8);   udp[11] = (u8)seq;
    udp[12] = (u8)(chunk >> 8); udp[13] = (u8)chunk;
    udp[14] = (u8)(AUDIO_BYTES >> 8); udp[15] = (u8)AUDIO_BYTES;
    memcpy(udp + 16, payload, n);

    flen = 14u + total;
    Xil_DCacheFlushRange((UINTPTR)txbuf, flen);
    dma_start_nr(DMA_ETH_BASE, DMA_TX, (u32)(UINTPTR)txbuf, flen);
    tx_busy = 1;
}

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

/* The AXI DMA simple-mode transfer length is limited by c_sg_length_width
   (default 14 bits): writing 19200 to R_LEN reads back 0x0B00 = 2816, i.e.
   19200 - 16384, so 85% of every 100 ms buffer was silently dropped.  Feed the
   I2S channel in chunks that stay below 16384 bytes instead. */
#define PB_CHUNK_WORDS 1200u    /* 4800 B; PB_WORDS / PB_CHUNK_WORDS == 4 */

/* ---------- on-device denoise (RNNoise, 480 samples @ 48 kHz) ----------
 * The electret sits near -19 dBFS while RNNoise is trained around -6..-12
 * dBFS, so the frame is lifted by RN_PRE before the network and the gain is
 * divided back out afterwards.  That keeps the bypass and the denoised branch
 * at the same loudness, which is what makes an A/B honest.
 * Mode is latched from the PS UART: '0' = bypass, '1' = RNNoise. */
#define RN_PRE 4.0f
#define UART0_SR   0xE000002Cu
#define UART0_FIFO 0xE0000030u

static DenoiseState *g_st;
static float g_fin[FRAME_WORDS];
static float g_fout[FRAME_WORDS];
static u32 g_dsp_mode;          /* 0 = bypass, 1 = RNNoise, 2 = gate        */
static u32 g_tone_en;           /* 0 = stay silent when idle (default)      */
static u32 g_dsp_ticks;         /* cost of the last denoised frame, in ticks */

static const char *dsp_name(u32 m)
{
    return (m == 1u) ? "RNNOISE" : (m == 2u) ? "GATE" : "BYPASS";
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

static void mic_process(u32 *mb)
{
    int k;

    if (g_noise) {
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

    if (g_dsp_mode == 1u && g_st) {
        u32 t0, t1;
        for (k = 0; k < (int)FRAME_WORDS; k++) {
            g_fin[k] = (float)(s32)(s16)(mb[k] & 0xFFFFu) * RN_PRE;
        }
        t0 = Xil_In32(TIMER_LOW);
        rnnoise_process_frame(g_st, g_fout, g_fin);
        t1 = Xil_In32(TIMER_LOW);
        g_dsp_ticks = t1 - t0;
        for (k = 0; k < (int)FRAME_WORDS; k++) {
            s32 v = (s32)(g_fout[k] * ((float)MIC_GAIN / RN_PRE));
            if (v > 32767) v = 32767;
            if (v < -32768) v = -32768;
            mb[k] = ((u32)((u16)v) << 16) | ((u16)v & 0xFFFFu);
        }
    } else if (g_dsp_mode == 2u) {
        u32 t0 = Xil_In32(TIMER_LOW);
        gate_apply(mb);
        g_dsp_ticks = Xil_In32(TIMER_LOW) - t0;
    } else {
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
}

/* non-blocking look at the PS console: '0' bypass, '1' RNNoise, '2' gate */
static void mic_mode_poll(void)
{
    while (!(Xil_In32(UART0_SR) & 0x1u)) {
        u8 c = (u8)(Xil_In32(UART0_FIFO) & 0xFFu);
        if (c >= '0' && c <= '2') {
            g_dsp_mode = (u32)(c - '0');
            if (g_dsp_mode == 2u) gate_reset();
            xil_printf("dsp=%u %s\r\n", g_dsp_mode, dsp_name(g_dsp_mode));
        } else if (c == 't') {
            g_tone_en ^= 1u;
            xil_printf("tone=%u\r\n", g_tone_en);
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
                                if (c >= '0' && c <= '2') {
                                    g_dsp_mode = (u32)(c - '0');
                                    if (g_dsp_mode == 2u) gate_reset();
                                    xil_printf("dsp=%u %s\r\n", g_dsp_mode,
                                               dsp_name(g_dsp_mode));
                                } else if (c == 't') {
                                    g_tone_en ^= 1u;
                                    xil_printf("tone=%u\r\n", g_tone_en);
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
                                    xil_printf("noise=%u\r\n", g_noise);
                                } else if (c == 'n') {
                                    g_noise = g_noise ? 0u : 800u;
                                    g_nz_l = 0;
                                    g_nz_r = 0;
                                    xil_printf("noise=%u\r\n", g_noise);
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
                    mic_mode_poll();
                    mic_process(mb);
                    rms = 0;
                    for (k = 0; k < (int)FRAME_WORDS; k++) {
                        s32 l = (s16)(mb[k] & 0xFFFFu);
                        rms += (u32)((l * l) >> 12);
                    }
                    rms /= FRAME_WORDS;

                    memcpy(pl, (u8 *)mb, CHUNK_BYTES);
                    send_udp(pl, CHUNK_BYTES, n_mic, 0, PORT_MIC);
                    memcpy(pl, (u8 *)mb + CHUNK_BYTES, CHUNK_BYTES);
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
           (xsdb dumps all 128 words in one go, after the run). */
        if ((n_mic % 500u) == 0u && n_mic != 0u && res[1] != n_mic) {
            res[1]  = n_mic;     res[2] = n_dac;      res[3] = n_under;
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
                           "pg=%u/%u/%u txst=%u clob=%u dsp=%u nz=%u tk=%u gt=%u/%u/%u\r\n",
                           n_mic, n_dac, n_under, n_pkt, rms, g_frame_dt, g_dac_dt,
                           res[56], res[57], res[60],
                           res[10], res[11], res[12],
                           res[18], res[19], res[20],
                           g_switch, g_lead2, g_tone_in, g_tone_out,
                           g_pgap8, g_pgap15, g_pgap_max, g_txstall, g_app_over,
                           g_dsp_mode, g_noise, g_dsp_ticks,
                           (u32)g_gate_prime, (u32)g_gate_nf,
                           (u32)(g_gate_g * 1000.0f));
            }
            g_pkt_seen = n_pkt;
            g_chk_sum = 0; g_chk_n = 0;              /* per-window averages */
            g_bnd_sum = 0; g_bnd_n = 0; g_bnd_win_max = 0;
        }
    }
    return 0;
}
