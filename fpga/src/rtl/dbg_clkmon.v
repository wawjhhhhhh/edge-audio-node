//=============================================================================
// dbg_clkmon.v  --  board-level debug: is the I2S clock domain alive?
//
// The 2026-11-02 regression left audio_axis_core_0 with m_axis_tvalid stuck at
// 0 while the DAC/MM2S side still completed blocks.  m_axis_tvalid is only
// ~rx_fifo_empty, the RX FIFO is only written by rx_push (aud_bclk domain,
// gated by rst_b_n which itself needs aud_bclk to toggle).  So the single
// question that splits every hypothesis is: do aud_bclk / aud_lrc toggle?
//
// This module samples both in the sys_clk (FCLK_CLK1, 50 MHz) domain and keeps
// free-running 8-bit counters.  Read the debug word twice a few hundred ms
// apart: any counter that CHANGES proves its input is toggling.  Free-running
// (not saturating) on purpose - a saturated counter cannot show liveness.
//
// bclk = 1.536 MHz (48k x 32) and lrc = 48 kHz, both far below clk/2, so a
// 2-stage synchroniser plus edge detect cannot miss or double count.
//=============================================================================
`timescale 1ns / 1ps

module dbg_clkmon (
    input  wire        clk,        // sys_clk domain (FCLK_CLK1, 50 MHz)
    input  wire        rst_n,      // sys_clk domain reset, active low
    input  wire        aud_bclk,   // async I2S bit clock
    input  wire        aud_lrc,    // async I2S word clock
    output wire [15:0] dbg         // {lrc_cnt[7:0], bclk_cnt[7:0]}
);

    reg [2:0] bclk_sr;
    reg [2:0] lrc_sr;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            bclk_sr <= 3'b000;
            lrc_sr  <= 3'b000;
        end else begin
            bclk_sr <= {bclk_sr[1:0], aud_bclk};
            lrc_sr  <= {lrc_sr[1:0],  aud_lrc};
        end
    end

    wire bclk_edge = bclk_sr[2] ^ bclk_sr[1];
    wire lrc_edge  = lrc_sr[2]  ^ lrc_sr[1];

    reg [7:0] bclk_cnt;
    reg [7:0] lrc_cnt;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            bclk_cnt <= 8'd0;
            lrc_cnt  <= 8'd0;
        end else begin
            if (bclk_edge) bclk_cnt <= bclk_cnt + 8'd1;
            if (lrc_edge)  lrc_cnt  <= lrc_cnt  + 8'd1;
        end
    end

    assign dbg = {lrc_cnt, bclk_cnt};

endmodule
