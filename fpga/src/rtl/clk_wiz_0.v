// clk_wiz_0.v -- 50 MHz -> 12.288 MHz MCLK generator for the ES8388.
//
// The audio core (work/audio_axis/rtl/audio_axis_core.v) instantiates an IP
// called clk_wiz_0 (it was generated inside the audio_axis project).  The
// merged eth_audio project does not carry that .xci, so this module provides
// the same interface with a plain MMCME2_BASE:
//
//   50 MHz * 24 / 1 = 1200 MHz VCO,  / 97.625 = 12.292 MHz  (+325 ppm)
//
// +325 ppm = 0.033 % = 48.016 kHz LRC instead of 48.000 kHz.  That is far
// below the ~1 cent (578 ppm) audibility threshold; the PC side plays at a
// fixed 48 kHz so the receive buffer drifts by one 10 ms frame per ~30 s,
// which the player absorbs (it drops/pads on overrun/underrun).
//
// If the exact rate ever matters, change CLKOUT0_DIVIDE_F here and keep the
// core's BCLK = MCLK/4, LRC = BCLK/64 relationship intact.
`timescale 1ns / 1ps

module clk_wiz_0 (
    input  wire clk_in1,      // 50 MHz
    input  wire reset,
    output wire clk_out1,     // 12.292 MHz
    output wire locked
);

    wire clkfb_in, clkfb_out;

    MMCME2_BASE #(
        .BANDWIDTH          ("OPTIMIZED"),
        .CLKFBOUT_MULT_F    (24.000),
        .CLKFBOUT_PHASE     (0.000),
        .CLKIN1_PERIOD      (20.000),      // 50 MHz
        .CLKOUT0_DIVIDE_F   (97.625),      // 1200 / 97.625 = 12.292 MHz
        .CLKOUT0_DUTY_CYCLE (0.500),
        .CLKOUT0_PHASE      (0.000),
        .DIVCLK_DIVIDE      (1),
        .STARTUP_WAIT       ("FALSE"),
        .REF_JITTER1        (0.010)
    ) u_mmcm (
        .CLKOUT0   (clk_out1),
        .CLKFBOUT  (clkfb_out),
        .CLKFBIN   (clkfb_in),
        .CLKIN1    (clk_in1),
        .PWRDWN    (1'b0),
        .RST       (reset),
        .LOCKED    (locked)
    );

    BUFG u_fb (.I(clkfb_out), .O(clkfb_in));

endmodule
