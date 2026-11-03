// eth_audio_top.v -- board-level wrapper: TEMAC (axi_ethernet, RGMII) + audio core.
//
// Same MDIO IOBUF trick as eth_axi_top.v: axi_ethernet exposes MDIO as three
// unidirectional nets, so the F19 bidirectional pin is built here.
//
// The audio_axis_core lives inside the BD (its own 50 MHz domain from FCLK1,
// internal MMCM makes 12.288 MHz MCLK, internal I2C configures the ES8388 and
// its IOBUF handles aud_sda), so the audio pins are simply forwarded.
//
// Do NOT re-register rgmii_txd / rgmii_tx_ctl in fabric: the IP drives them
// from internal ODDRs and Vivado rejects it with [DRC REQP-1884].
module eth_audio_top (
    // RGMII to the PL-side YT8531C (U13)
    input  wire        eth_rxc,        // K17
    input  wire        eth_rx_ctl,     // E17
    input  wire [3:0]  eth_rxd,        // B19 A20 H17 H16
    output wire        eth_txc,        // B20
    output wire        eth_tx_ctl,     // K18
    output wire [3:0]  eth_txd,        // D18 C20 D19 D20
    output wire        eth_mdc,        // F20
    inout  wire        eth_mdio,       // F19
    output wire        eth_rst_n,      // G15 -> PHYRSTB (no pull-up on board)
    // console on the PL side of the CH340 jumper
    output wire        uart_txd,       // J15
    input  wire        uart_rxd,       // T19
    // ES8388 / I2S
    output wire        aud_mclk,       // E19  12.288 MHz
    output wire        aud_bclk,       // M18  3.072 MHz
    output wire        aud_lrc,        // L20  48 kHz
    output wire        aud_dacdat,     // G17
    input  wire        aud_adcdat,     // M17
    output wire        aud_scl,        // E18
    inout  wire        aud_sda,        // F17
    // Copy of the I2S bus for the external DAC (route B: 40-pin header J4,
    // bank 13). Pure fan-out of the same nets the onboard ES8388 sees.
    output wire        aud2_bclk,      // header W11
    output wire        aud2_lrc,       // header Y13
    output wire        aud2_dacdat,    // header Y11
    // Master clock for the external DAC.  Not required by this module (it has
    // its own oscillator), kept because it costs one ball and lets an MCLK-clocked
    // carrier be tried later.  12.288 MHz == 256*fs.
    output wire        aud2_mclk,      // header W10
    // Two extra copies of the DATA line on header balls that used to be the
    // (never-acknowledged) I2C pair.  Y11 above is the ball the wiring table
    // says DATA belongs on, but the module stays silent there while the very
    // same signal plays on W9/W8 -- so both copies are kept and the flying
    // lead can sit on whichever one works.  Cleanup: these are plain DATA
    // fan-outs now, not I2C, and PULLUP is gone from the XDC.
    output wire        aud2_dacdat_w9, // header W9
    output wire        aud2_dacdat_w8, // header W8
    // Spare PL status LEDs.  led0 is dark; led1 blinks at 1.365 s as a plain
    // "aud_mclk is running" heartbeat (the DAC bring-up is done, so the old
    // I2C pass/NACK semantics are gone).
    output wire        led0,           // H15  PL_LED0, active high
    output wire        led1            // L15  PL_LED1, active high
);

    wire mdio_i, mdio_o, mdio_t;

    eth_audio_wrapper u_bd (
        .eth_rxc     (eth_rxc),
        .eth_rx_ctl  (eth_rx_ctl),
        .eth_rxd     (eth_rxd),
        .eth_txc     (eth_txc),
        .eth_tx_ctl  (eth_tx_ctl),
        .eth_txd     (eth_txd),
        .eth_mdc     (eth_mdc),
        .eth_mdio_i  (mdio_i),
        .eth_mdio_o  (mdio_o),
        .eth_mdio_t  (mdio_t),
        .eth_rst_n   (eth_rst_n),
        .uart_txd    (uart_txd),
        .uart_rxd    (uart_rxd),
        .aud_mclk    (aud_mclk),
        .aud_bclk    (aud_bclk),
        .aud_lrc     (aud_lrc),
        .aud_dacdat  (aud_dacdat),
        .aud_adcdat  (aud_adcdat),
        .aud_scl     (aud_scl),
        .aud_sda     (aud_sda),
        .aud2_dacdat (unused_dac_b)   /* 2026-11-03：BD 的 dac_split 出口不再直接上脚 */
        /* dac_stat 已从 BD 删除（2026-11-03 干净重建）：它是 txgen 的调试字，
           本层从来没有消费过它，留着只是一个隐式 1 bit 线 + 一条 Synth 8-11241 提示。 */
    );

    IOBUF u_mdio (.IO(eth_mdio), .I(mdio_o), .O(mdio_i), .T(mdio_t));

    /* 2026-11-02 包6 -- 双 DAC 输出模式。
       原来这里是 assign aud2_dacdat = aud_dacdat（纯扇出，两口永远同源）。
       现在 BD 里插了一个 dac_split（模块引用），把 audio_axis_core 的串行数据
       按 LRC 门控成两路：
         M0 同源 / M1 板载只左、外接只右 / M2 板载静音、外接完整
       模式由 AXI GPIO 第二通道（0x41210000，2 bit）驱动。
       所以 aud2_dacdat 现在来自 u_bd 的输出端口，不再在本层赋值。 */
    assign aud2_bclk   = aud_bclk;
    /* 2026-11-03：外接模块的 LRCK 到底落在 Y11 还是 Y13 一直没定论（10-29 的表按
       丝印把 LRCK 记在 Y11、DATA 记在 Y13；后来 DATA 改到 W9/W8，Y13 就空出来了）。
       与其再猜，不如两个脚都送 LRCK：模块的 LRCK 无论接哪个脚都能拿到帧时钟，
       而 DATA 只从 W9/W8 出，不存在歧义。BD 的 dac_split 右声道出口接在这里不用。 */
    assign aud2_lrc    = aud_lrc;
    assign aud2_dacdat = unused_dac_b;  /* Y11 : DATA (dac_split right) */
    assign aud2_mclk   = aud_mclk;
    wire   unused_dac_b;

    /* EXPERIMENT C (2026-10-28 22:xx) -- control run, no I2C at all.
       Merchant confirms: the module is an I2S *slave*, has its own crystal
       (so no MCLK is needed), accepts 32-bit words up to 768 kHz, and plays
       out of the box on its factory-default registers.
       We therefore stop touching the bus entirely: both pins are released to
       high-Z so the module's own controller owns SCL/SDA.  The stream is
       unchanged (32-bit slots, 3.072 MHz / 64 BCK per frame, I2S).
       Interpretation of the result:
         plays  -> our es9038_cfg writes were the damage; bisect the table.
         silent -> registers are exonerated for good, and the fault is in the
                   delivery (pin order / levels / ground / frame shape).

       2026-10-29 00:xx -- POST-BRING-UP CLEANUP.  The ES9038Q2M module plays
       from its factory-default registers (its own crystal, I2S slave, no MCLK
       needed), so the I2C master, its register table and the LED read-out are
       all gone.  What is left is: the I2S fan-out (incl. two extra DATA copies
       on W9/W8, because this module ignores DATA on Y11) and a PL heartbeat
       that the PS can read over JTAG with `mrd -force 0x41200008`. */
    wire [31:0] dac_stat;

    /* Internal power-on reset: the top level has no reset port.
       BUG FIXED 2026-10-28 22:5x.  This used to be `wire dac_rst_n = &por_cnt`.
       por_cnt is free-running, so that expression is high for exactly ONE cycle
       out of every 256 and low the rest of the time: every register of
       es9038_cfg was asynchronously cleared again on the very next clock, so
       the I2C state machine sat in idle for ever (measured: stat bus returned
       0x3000 for 200 consecutive reads, tick and cfg_done never moved).  Every
       I2C conclusion drawn between 14:50 and 22:30 today was therefore drawn
       about a master that never issued a single transaction.  Latch it. */
    reg  [7:0] por_cnt;
    reg        dac_rstn_q = 1'b0;
    always @(posedge aud_mclk) por_cnt <= por_cnt + 8'd1;
    always @(posedge aud_mclk) if (!dac_rstn_q) dac_rstn_q <= &por_cnt;
    wire dac_rst_n = dac_rstn_q;

    /* 2026-10-29 00:xx -- XDC CLEANUP BUILD.
       DATA is fanned out to Y11 (the ball the wiring table specifies) and to
       W9/W8 (the balls that actually work on this module).  Names, comments
       and the XDC now say "DATA", not "I2C": the register table is gone for
       good -- the module plays from its factory defaults. */
    assign aud2_dacdat_w9 = unused_dac_b;  // W9 : DATA（dac_split 右声道出口）
    assign aud2_dacdat_w8 = unused_dac_b;  // W8 : DATA 副本（同一根线，备用）

    reg [23:0] alive_cnt;
    always @(posedge aud_mclk) alive_cnt <= alive_cnt + 24'd1;
    assign led0 = 1'b0;
    assign led1 = alive_cnt[23];          // 1.365 s blink == aud_mclk is alive

    /* axi_gpio_0 channel 2 (JTAG `mrd -force 0x41200008`) now carries a PL
       heartbeat instead of the dead I2C status word: 0xA5 in the top byte,
       a free-running 24-bit counter below.  If the counter moves, the fabric
       and aud_mclk are alive; if it reads 0, the PL is not configured. */
    assign dac_stat = {8'hA5, alive_cnt[23:0]};

endmodule
