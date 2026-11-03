//=============================================================================
// audio_axis_core.v --- I2S <-> AXI4-Stream bridge (reusable core)
//
// This module is the part that will sit inside the block design next to the
// AXI DMA.  It is NOT the chip top level: audio_axis_top.v wraps it for the
// stand-alone "PL self-loop" bring-up build.
//
// Data format on AXIS: one 32-bit beat per LRC period
//     [31:16] = right channel sample
//     [15: 0] = left  channel sample
// TLAST asserts on every BEATS_PER_PKT-th beat (48 beats = 1 ms @ 48 kHz).
//
// Clock domains:
//     sys_clk  (50 MHz) : I2C config, AXIS master/slave, FIFO read sides
//     aud_mclk (12.288M) : I2S clock generation (MMCM)
//     aud_bclk (3.072M) : I2S receive / transmit, FIFO write sides
//
// Ports aud_lrc / aud_bclk / aud_mclk / aud_dacdat are PL outputs: the PL is
// the I2S master, the ES8388 is the slave.
//=============================================================================
`timescale 1ns / 1ps

module audio_axis_core #(
    parameter WL            = 16,
    parameter BEATS_PER_PKT = 48,
    parameter LOOPBACK      = 1,
    parameter AW            = 4
)(
    input  wire        sys_clk,     // 50 MHz  (U18)
    input  wire        sys_rst_n,   // active low (N16)

    // ---- I2S to ES8388 (PL is master) ----
    output wire        aud_lrc,     // (L20)  48 kHz
    output wire        aud_bclk,    // (M18)  3.072 MHz
    output wire        aud_mclk,    // (E19)  12.288 MHz
    input  wire        aud_adcdat,  // (M17)
    output wire        aud_dacdat,  // (G17)

    // ---- ES8388 control ----
    output wire        aud_scl,     // (E18)
    inout  wire        aud_sda,     // (F17)

    // ---- AXI4-Stream: captured audio out ----
    output wire [31:0] m_axis_tdata,
    output wire        m_axis_tvalid,
    output wire        m_axis_tlast,
    input  wire        m_axis_tready,

    // ---- AXI4-Stream: playback audio in ----
    input  wire [31:0] s_axis_tdata,
    input  wire        s_axis_tvalid,
    input  wire        s_axis_tlast,
    output wire        s_axis_tready,

    // ---- observability ----
    output wire        stat_rx_ovf, // RX FIFO overflowed: samples were dropped
    output wire        stat_tx_unf, // TX FIFO underran: silence was inserted

    // ---- PS visible control/status ----
    input  wire        key,         // PL push button (L14), active low
    output wire [31:0] gpio_word,   // {cnt_rx_ovf[7:0], cnt_tx_unf[6:0], key_state, 16'h1022} -> axi_gpio_0/gpio_io_i
    // I2C bring-up instrumentation: {i2c_done_cnt[15:0], i2c_exec_cnt[15:0]}
    output wire [31:0] dbg_audio
);

    //=========================================================================
    // 1. Clocks and I2S timing
    //=========================================================================
    wire locked;
    clk_wiz_0 u_clk_wiz_0 (
        .clk_out1 (aud_mclk),
        .reset    (~sys_rst_n),
        .locked   (locked),
        .clk_in1  (sys_clk)
    );

    //=========================================================================
    // 1b. PL push button (L14, active low) -> word readable from the PS
    //=========================================================================
    // Exposed through axi_gpio_0 (32-bit, all inputs).  Low half keeps the
    // bring-up marker 0x1022, bit 16 mirrors the (debounced) button state.
    reg [2:0] key_sr;
    always @(posedge sys_clk or negedge sys_rst_n) begin
        if (!sys_rst_n) key_sr <= 3'b111;
        else            key_sr <= {key_sr[1:0], key};
    end
    wire key_state = key_sr[2];          // 1 = released, 0 = pressed
    // ---- instrumentation: count PL-side data path faults ------------------
    // These two counters are the ground truth for "is the PL dropping or
    // inserting samples".  Both flags are pulses in the aud_bclk domain (325 ns
    // wide), so they are synchronised through 2 FFs and edge-detected before
    // being counted in sys_clk.
    reg [1:0]  rx_ovf_sr, tx_unf_sr;
    reg [15:0] cnt_rx_ovf, cnt_tx_unf;
    always @(posedge sys_clk or negedge sys_rst_n) begin
        if (!sys_rst_n) begin
            rx_ovf_sr  <= 2'b00;
            tx_unf_sr  <= 2'b00;
            cnt_rx_ovf <= 16'd0;
            cnt_tx_unf <= 16'd0;
        end else begin
            rx_ovf_sr <= {rx_ovf_sr[0], stat_rx_ovf};
            tx_unf_sr <= {tx_unf_sr[0], stat_tx_unf};
            if (rx_ovf_sr == 2'b01) cnt_rx_ovf <= cnt_rx_ovf + 16'd1;
            if (tx_unf_sr == 2'b01) cnt_tx_unf <= cnt_tx_unf + 16'd1;
        end
    end

    // [31:24] = rx overflow count (8 bits), [23:17] = tx underrun count (7 bits),
    // [16] = key released, [15:0] = 0x1022 (the "Oct 22" marker).
    assign gpio_word = {cnt_rx_ovf[7:0], cnt_tx_unf[6:0], key_state, 16'h1022};

    slave_signal_gen u_slave_signal_gen (
        .clk      (aud_mclk),
        .rst_n    (sys_rst_n),
        .aud_bclk (aud_bclk),
        .aud_lrc  (aud_lrc)
    );

    //=========================================================================
    // 2. ES8388 register configuration (I2C)
    //    Register table is the michp2 variant:
    //      R4  = 0x30  -> headphone outputs LOUT1/ROUT1 only
    //      R10 = 0x00  -> ADC input = LINPUT1/RINPUT1 = onboard MIC
    //      R46/R47 volume = 33 (max)
    //=========================================================================
    //    Hold the config block in reset for ~21 ms after reset release so the
    //    MMCM has locked and aud_mclk is present before the first I2C write.
    //    ES8388 ignores register writes that arrive without MCLK running.
    //=========================================================================
    reg [19:0] cfg_dly;
    always @(posedge sys_clk or negedge sys_rst_n) begin
        if (!sys_rst_n)            cfg_dly <= 20'd0;
        else if (!cfg_dly[19])     cfg_dly <= cfg_dly + 1'b1;
    end
    wire cfg_rst_n = cfg_dly[19];

    audio_config #(
        .WL (WL)
    ) u_audio_config (
        .clk     (sys_clk),
        .rst_n   (cfg_rst_n),
        .aud_scl (aud_scl),
        .aud_sda (aud_sda)
    );

    //=========================================================================
    // 3. Reset synchroniser into the aud_bclk domain
    //=========================================================================
    reg [3:0] rst_b_sr;
    always @(posedge aud_bclk or negedge sys_rst_n) begin
        if (!sys_rst_n) rst_b_sr <= 4'b0000;
        else            rst_b_sr <= {rst_b_sr[2:0], 1'b1};
    end
    wire rst_b_n = rst_b_sr[3];

    //=========================================================================
    // 4. I2S receive -> 32-bit {R,L} word -> RX FIFO (bclk -> sys_clk)
    //=========================================================================
    wire [WL-1:0] adc_data;
    wire          rx_done;

    audio_receive #(
        .WL (WL)
    ) u_audio_receive (
        .rst_n     (rst_b_n),
        .aud_bclk  (aud_bclk),
        .aud_lrc   (aud_lrc),
        .aud_adcdat(aud_adcdat),
        .rx_done   (rx_done),
        .adc_data  (adc_data)
    );

    // rx_done fires once per LRC half period, ~17 BCLK after the slot edge.
    // aud_lrc still holds that slot's level, so it selects L or R.
    reg [WL-1:0] ch_l;
    reg [31:0]   rx_word;
    reg          rx_push;

    always @(posedge aud_bclk or negedge rst_b_n) begin
        if (!rst_b_n) begin
            ch_l    <= {WL{1'b0}};
            rx_word <= 32'd0;
            rx_push <= 1'b0;
        end else begin
            rx_push <= 1'b0;
            if (rx_done) begin
                if (aud_lrc == 1'b0) begin
                    ch_l <= adc_data;               // left slot captured
                end else begin
                    rx_word <= {adc_data, ch_l};    // {R, L}
                    rx_push <= 1'b1;
                end
            end
        end
    end

    wire [31:0] rx_fifo_dout;
    wire        rx_fifo_empty;
    wire        rx_fifo_full;
    wire        rx_pop;

    afifo #(.DW(32), .AW(AW)) u_rx_fifo (
        .wclk  (aud_bclk),
        .wrst_n(rst_b_n),
        .wpush (rx_push),
        .wdata (rx_word),
        .wfull (rx_fifo_full),
        .rclk  (sys_clk),
        .rrst_n(sys_rst_n),
        .rpop  (rx_pop),
        .rdata (rx_fifo_dout),
        .rempty(rx_fifo_empty)
    );

    assign stat_rx_ovf = rx_push & rx_fifo_full;

    //=========================================================================
    // 5. AXIS master (sys_clk domain)
    //=========================================================================
    reg [15:0] beat_cnt;

    assign m_axis_tdata  = rx_fifo_dout;
    assign m_axis_tvalid = ~rx_fifo_empty;
    assign m_axis_tlast  = m_axis_tvalid & (beat_cnt == (BEATS_PER_PKT - 1));

    //=========================================================================
    // 6. AXIS slave -> TX FIFO (sys_clk -> bclk)
    //=========================================================================
    wire [31:0] tx_fifo_dout;
    wire        tx_fifo_empty;
    wire        tx_fifo_full;
    wire        tx_pop;

    assign s_axis_tready = ~tx_fifo_full;

    // In loopback the master output feeds the slave input directly inside the
    // PL; the AXIS ports are then unused and get trimmed by synthesis.
    wire        s_valid = LOOPBACK ? m_axis_tvalid : s_axis_tvalid;
    wire [31:0] s_data  = LOOPBACK ? rx_fifo_dout  : s_axis_tdata;
    wire        s_ready = s_axis_tready;

    // Which "ready" releases the RX FIFO:
    //   LOOPBACK=1 -> the internal slave consumes the beat (loop inside PL)
    //   LOOPBACK=0 -> the external AXIS master (DMA S2MM) consumes it
    // Using s_ready unconditionally would pop the RX FIFO even when the DMA is
    // not reading, silently dropping samples and misaligning tlast/beat_cnt.
    wire m_ready = LOOPBACK ? s_ready : m_axis_tready;
    assign rx_pop = m_axis_tvalid & m_ready;

    wire tx_push = s_valid & s_ready;

    afifo #(.DW(32), .AW(AW)) u_tx_fifo (
        .wclk  (sys_clk),
        .wrst_n(sys_rst_n),
        .wpush (tx_push),
        .wdata (s_data),
        .wfull (tx_fifo_full),
        .rclk  (aud_bclk),
        .rrst_n(rst_b_n),
        .rpop  (tx_pop),
        .rdata (tx_fifo_dout),
        .rempty(tx_fifo_empty)
    );

    always @(posedge sys_clk or negedge sys_rst_n) begin
        if (!sys_rst_n)
            beat_cnt <= 16'd0;
        else if (rx_pop)
            beat_cnt <= (beat_cnt == (BEATS_PER_PKT - 1)) ? 16'd0 : (beat_cnt + 16'd1);
    end

    //=========================================================================
    // 7. TX FIFO -> I2S transmit (bclk domain)
    //
    // One 32-bit word is popped at the START of the left slot and held for the
    // whole LRC period, so the left slot sends word[15:0] and the right slot
    // sends word[31:16].  This is the fix for the offset that made the vendor
    // mono loopback swap channels: there is no "latch one slot late" here.
    //=========================================================================
    reg [31:0] tx_word;
    reg [5:0]  tx_bit;
    reg        lrc_d;
    reg        tx_ch;
    reg        dacdat_r;

    wire tx_edge = aud_lrc ^ lrc_d;

    assign tx_pop = tx_edge & (aud_lrc == 1'b0) & ~tx_fifo_empty;

    always @(posedge aud_bclk or negedge rst_b_n) begin
        if (!rst_b_n) begin
            lrc_d   <= 1'b0;
            tx_bit  <= 6'd0;
            tx_ch   <= 1'b0;
            tx_word <= 32'd0;
        end else begin
            lrc_d <= aud_lrc;
            if (tx_edge) begin
                tx_bit <= 6'd0;
                tx_ch  <= aud_lrc;
                if (aud_lrc == 1'b0)
                    tx_word <= tx_fifo_empty ? 32'd0 : tx_fifo_dout;
            end else if (tx_bit <= WL) begin
                tx_bit <= tx_bit + 6'd1;
            end
        end
    end

    // data changes on the falling edge so it is stable for the rising edge,
    // matching the Philips I2S MSB-one-bit-after-LRC convention
    always @(negedge aud_bclk or negedge rst_b_n) begin
        if (!rst_b_n)
            dacdat_r <= 1'b0;
        else if (tx_bit < WL)
            dacdat_r <= tx_ch ? tx_word[31 - tx_bit] : tx_word[15 - tx_bit];
        else
            dacdat_r <= 1'b0;
    end

    assign aud_dacdat  = dacdat_r;
    assign stat_tx_unf = tx_edge & (aud_lrc == 1'b0) & tx_fifo_empty;


    // 9. I2C bring-up instrumentation (eth_audio debug build)
    //
    // Watches the aud_scl pin the core itself drives. If the ES8388 config
    // FSM never runs, SCL never toggles and the codec stays powered down,
    // which is exactly what an ADC reading back near-zero looks like.
    // No hierarchical references here, so it survives synthesis.
    //=========================================================================
    reg        scl_d;
    reg [15:0] scl_fall_cnt;
    reg [15:0] sda_low_cnt;

    always @(posedge sys_clk or negedge sys_rst_n) begin
        if (!sys_rst_n) begin
            scl_d        <= 1'b1;
            scl_fall_cnt <= 16'd0;
            sda_low_cnt  <= 16'd0;
        end else begin
            scl_d <= aud_scl;
            if (scl_d && !aud_scl) begin
                scl_fall_cnt <= scl_fall_cnt + 16'd1;
                if (!aud_sda) sda_low_cnt <= sda_low_cnt + 16'd1;
            end
        end
    end

    assign dbg_audio = {sda_low_cnt, scl_fall_cnt};

endmodule
