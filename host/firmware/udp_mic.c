/* udp_mic.c -- 板载 MIC -> UDP -> PC 实时监听（合并版 eth_audio 设计）
 *
 * 数据通路：
 *   MIC -> ES8388 ADC -> I2S -> audio_axis_core -> AXIS -> AXI DMA1(S2MM) -> DDR
 *   -> PS 取 480 个 32 bit 字（=10 ms 立体声）-> 拆 2 个 UDP 包 -> 100 Mbps -> PC
 *
 * UDP 载荷格式（与 udp_play.py 一致）：
 *   8 B 头：'A','U', seq_hi,seq_lo, chunk_hi,chunk_lo, total_hi,total_lo
 *   960 B PCM：48 kHz / 16 bit / 立体声，小端，[15:0]=左 [31:16]=右
 *
 * DMA 0 = 以太网（MM2S 发, S2MM 收），DMA 1 = 音频（S2MM 收 MIC）。
 */
#include <stdio.h>
#include <string.h>
#include "xil_io.h"
#include "xil_printf.h"
#include "xil_cache.h"
#include "xparameters.h"
#include "sleep.h"

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

#define MAC0   0x02u
#define MAC5   0x01u
#define BOARD_IP0 169u
#define BOARD_IP1 254u
#define BOARD_IP2 191u
#define BOARD_IP3 50u
#define UDP_PORT 50505u

#define AUDIO_BYTES   1920u        /* one 10 ms stereo frame */
#define CHUNK_BYTES   960u         /* 240 stereo samples     */
#define HDR_BYTES     8u
#define UDP_PAYLOAD   (HDR_BYTES + CHUNK_BYTES)
#define WORDS_PER_FRAME 480u       /* 480 x 32 bit = 1920 B  */

static u8 txbuf[1514] __attribute__((aligned(64)));
static u8 rxbuf[2048] __attribute__((aligned(64)));
/* Two frame buffers so the next S2MM can be armed before the current frame is
   processed.  With a single buffer the DMA stayed idle during memcpy + the two
   UDP sends, the 16-beat audio FIFO overflowed and ~2 ms of audio were lost
   every frame (83 fps instead of 100). */
static u32 micbuf[2][WORDS_PER_FRAME] __attribute__((aligned(64)));
static u8 pl[UDP_PAYLOAD] __attribute__((aligned(64)));

#ifndef MIC_GAIN
#define MIC_GAIN 8
#endif

/* ---------------- DMA ---------------- */
static void dma_start(u32 base, u32 off, u32 addr, u32 len)
{
    u32 sr;
    Xil_Out32(base + off + R_CR, CR_RESET);
    while (Xil_In32(base + off + R_CR) & CR_RESET) { }
    sr = Xil_In32(base + off + R_SR);
    Xil_Out32(base + off + R_SR, sr | SR_IOC | SR_ERR);   /* clear sticky */
    Xil_Out32(base + off + R_CR, CR_RUNSTOP);
    Xil_Out32(base + off + R_ADDR, addr);
    Xil_Out32(base + off + R_LEN, len);
}

static int dma_wait(u32 base, u32 off, u32 tries_ms, u32 *len)
{
    u32 sr = 0;
    u32 spin, budget = tries_ms * 100000u;   /* ~0.1 ms per 100k spins at 666 MHz */

    /* Tight poll: usleep(1000) here cost a full millisecond per frame, which
       pushed the MIC loop from 100 fps down to 83 fps and let the 16-beat
       audio FIFO overflow between frames. */
    for (spin = 0; spin < budget; spin++) {
        sr = Xil_In32(base + off + R_SR);
        if (sr & SR_ERR) return -1;
        if (sr & SR_IOC) { if (len) *len = Xil_In32(base + off + R_LEN); return 0; }
    }
    return -2;
}

/* ---------------- checksums ---------------- */
static u32 ip_csum(const u8 *p, int n)
{
    u32 s = 0;
    int i;
    for (i = 0; i + 1 < n; i += 2) s += ((u32)p[i] << 8) | p[i + 1];
    if (n & 1) s += (u32)p[i] << 8;
    while (s >> 16) s = (s & 0xFFFFu) + (s >> 16);
    return (~s) & 0xFFFFu;
}

/* ---------------- UDP frame build/send ---------------- */
static void build_udp_frame(const u8 *payload, u32 n, u32 seq, u32 chunk)
{
    static const u8 bcast[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };
    u8 *e = txbuf;
    u8 *ip, *udp;
    u32 total = 20u + 8u + n;

    memset(e, 0, 64);
    memcpy(e, bcast, 6);
    e[6] = MAC0; e[7] = 0x00; e[8] = 0x00; e[9] = 0x00; e[10] = 0x00; e[11] = MAC5;
    e[12] = 0x08; e[13] = 0x00;

    ip = e + 14;
    ip[0] = 0x45; ip[1] = 0x00;
    ip[2] = (u8)(total >> 8); ip[3] = (u8)total;
    ip[4] = 0x00; ip[5] = 0x01;                 /* id */
    ip[6] = 0x40; ip[7] = 0x00;                 /* DF */
    ip[8] = 64; ip[9] = 17;                     /* ttl, udp */
    ip[10] = 0; ip[11] = 0;
    ip[12] = BOARD_IP0; ip[13] = BOARD_IP1; ip[14] = BOARD_IP2; ip[15] = BOARD_IP3;
    ip[16] = 255; ip[17] = 255; ip[18] = 255; ip[19] = 255;   /* broadcast */
    {
        u32 c = ip_csum(ip, 20);
        ip[10] = (u8)(c >> 8); ip[11] = (u8)c;
    }

    udp = ip + 20;
    udp[0] = (u8)(UDP_PORT >> 8); udp[1] = (u8)UDP_PORT;
    udp[2] = (u8)(UDP_PORT >> 8); udp[3] = (u8)UDP_PORT;
    udp[4] = (u8)((8u + n) >> 8); udp[5] = (u8)(8u + n);
    udp[6] = 0; udp[7] = 0;                     /* no checksum (IPv4 ok) */

    /* payload: 8 B header + PCM */
    udp[8]  = 'A'; udp[9] = 'U';
    udp[10] = (u8)(seq >> 8);  udp[11] = (u8)seq;
    udp[12] = (u8)(chunk >> 8); udp[13] = (u8)chunk;
    udp[14] = (u8)(AUDIO_BYTES >> 8); udp[15] = (u8)AUDIO_BYTES;
    memcpy(udp + 16, payload, n);

    {
        u32 flen = 14u + total;
        u32 dl;
        Xil_DCacheFlushRange((UINTPTR)txbuf, flen);
        dma_start(DMA_ETH_BASE, DMA_TX, (u32)(UINTPTR)txbuf, flen);
        (void)dma_wait(DMA_ETH_BASE, DMA_TX, 50, &dl);
    }
}

/* ---------------- PHY: force 100 Mbps FD via AN advertisement ---- */
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

int main(void)
{
    u32 *res = (u32 *)RES_BASE;
    u32 len, n_frames = 0, n_fail = 0, n_drop = 0, i;
    u32 link = 0, wait_ms = 0;

    Xil_DCacheDisable();
    memset(res, 0, 64 * 4);
    res[0] = 0x4D494331u;                 /* 'MIC1' */

    xil_printf("\r\n=== MIC -> UDP stream (eth_audio) ===\r\n");

    /* --- PHY: advertise 100BASE-TX FD only, clear 1000BASE-T (reg 9) --- */
    mdio_init();
    xil_printf("p4 id=%04x:%04x bmcr=%04x bmsr=%04x\r\n",
               mdio_rd(4, 2), mdio_rd(4, 3), mdio_rd(4, 0), mdio_rd(4, 1));
    mdio_wr(4, 9, 0x0000);
    mdio_wr(4, 4, 0x0101);
    mdio_wr(4, 0, 0x1300);
    while (wait_ms < 3000u) {
        u32 bs = mdio_rd(4, 1), bs2 = mdio_rd(4, 1);
        if (bs2 & 0x0004u) { link = 1; break; }
        usleep(50000); wait_ms += 50u;
    }
    xil_printf("p4 AN100 bmcr=%04x bmsr=%04x link=%u wait=%u ms\r\n",
               mdio_rd(4, 0), mdio_rd(4, 1), link, wait_ms);
    {
        u32 e = Xil_In32(ETH_BASE + 0x410);
        e = (e & ~0xC0000000u) | 0x40000000u;      /* 100 Mb/s */
        Xil_Out32(ETH_BASE + 0x410, e);
        xil_printf("EMMC=%08x\r\n", Xil_In32(ETH_BASE + 0x410));
    }

    xil_printf("streaming MIC at 100 fps (gain=%d)\r\n", MIC_GAIN);

    {
    int cur = 0;

    Xil_DCacheInvalidateRange((UINTPTR)micbuf[cur], AUDIO_BYTES);
    dma_start(DMA_AUD_BASE, DMA_RX, (u32)(UINTPTR)micbuf[cur], AUDIO_BYTES);

    for (;;) {
        u32 rms = 0;
        int k;
        u32 *mb;

        if (dma_wait(DMA_AUD_BASE, DMA_RX, 100, &len) != 0) {
            n_drop++;
            Xil_DCacheInvalidateRange((UINTPTR)micbuf[cur], AUDIO_BYTES);
            dma_start(DMA_AUD_BASE, DMA_RX, (u32)(UINTPTR)micbuf[cur], AUDIO_BYTES);
            continue;
        }

        /* Ping-pong: switch to the buffer just filled and immediately re-arm
           the other one, so the S2MM never sits idle while the core keeps
           pushing beats into the 16-deep FIFO. */
        mb = micbuf[cur];
        cur ^= 1;
        Xil_DCacheInvalidateRange((UINTPTR)micbuf[cur], AUDIO_BYTES);
        dma_start(DMA_AUD_BASE, DMA_RX, (u32)(UINTPTR)micbuf[cur], AUDIO_BYTES);

        /* optional software gain with saturation (MIC level is low) */
#if MIC_GAIN > 1
        for (k = 0; k < (int)WORDS_PER_FRAME; k++) {
            s32 l = (s16)(mb[k] & 0xFFFFu) * MIC_GAIN;
            s32 r = (s16)((mb[k] >> 16) & 0xFFFFu) * MIC_GAIN;
            if (l > 32767) l = 32767; if (l < -32768) l = -32768;
            if (r > 32767) r = 32767; if (r < -32768) r = -32768;
            mb[k] = ((u32)((u16)r) << 16) | ((u16)l & 0xFFFFu);
        }
#endif
        for (k = 0; k < (int)WORDS_PER_FRAME; k++) {
            s32 l = (s16)(mb[k] & 0xFFFFu);
            rms += (u32)((l * l) >> 12);
        }
        rms /= WORDS_PER_FRAME;

        /* 2 packets of 960 B */
        memcpy(pl, (u8 *)mb, CHUNK_BYTES);
        build_udp_frame(pl, CHUNK_BYTES, (u16)n_frames, 0);
        memcpy(pl, (u8 *)mb + CHUNK_BYTES, CHUNK_BYTES);
        build_udp_frame(pl, CHUNK_BYTES, (u16)n_frames, 1);

        n_frames++;
        if ((n_frames % 100u) == 0u) {
            xil_printf("mic frames=%u drop=%u err=%u rms=%u\r\n",
                       n_frames, n_drop, n_fail, rms);
            res[1] = n_frames; res[2] = n_drop; res[3] = rms;
            Xil_DCacheFlushRange((UINTPTR)res, 64 * 4);
        }
        (void)i;
    }
    }
    return 0;
}
