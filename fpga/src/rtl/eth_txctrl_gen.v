// eth_txctrl_gen.v -- feed the axi_ethernet (PG138) transmit control stream
//
// PG138 "Transmit AXI4-Stream Interface":
//   * The Normal Transmit control frame ALWAYS contains SIX 32-bit control words (words 0..5).
//   * "the AXI4-Stream data ready signal is held not ready until an AXI4-Stream control stream
//      has been received" -> if s_axis_txc is never driven, s_axis_txd_tready never asserts.
//   * With C_TXCSUM = 0 none of the control words are *used*, but the burst still has to be sent.
//   * Control/data TSTRB is declared constant by the core: drive 0xF.
//
// Word 0 bits[31:28] = 1010 selects a Normal Transmit Frame; words 1..5 are don't-care here.
//
// 2026-10-25: the AXI DMA on this board refuses to leave reset (MM2S_DMACR reads back 0x10002,
// i.e. the self-clearing Reset bit stuck set, MM2S_DMASR = 0x1 Halted), so the DMA never drives
// s_axis_tvalid and we could not tell whether TEMAC/PHY/cable work.  A built-in frame generator
// was added: if the DMA stays silent for ~10 ms, this block emits ONE 64-byte broadcast frame by
// itself.  That isolates "TEMAC + PHY + cable + PC" from "DMA bring-up".

`timescale 1ns / 1ps

module eth_txctrl_gen #(
    parameter [31:0] CTRL_WORD0 = 32'hA000_0000,
    parameter [19:0] GEN_PERIOD  = 20'd1_000_000   // 10 ms @ 100 MHz
) (
    input  wire        clk,
    input  wire        rstn,

    // slave: frame data from DMA
    input  wire [31:0] s_axis_tdata,
    input  wire [3:0]  s_axis_tkeep,
    input  wire        s_axis_tvalid,
    input  wire        s_axis_tlast,
    output wire        s_axis_tready,

    // master: frame data to MAC
    output wire [31:0] m_axis_tdata,
    output wire [3:0]  m_axis_tkeep,
    output wire        m_axis_tvalid,
    output wire        m_axis_tlast,
    input  wire        m_axis_tready,

    // master: 6-word control burst to MAC
    output wire [31:0] c_axis_tdata,
    output wire [3:0]  c_axis_tkeep,
    output wire        c_axis_tvalid,
    output wire        c_axis_tlast,
    input  wire        c_axis_tready,

    output wire [31:0] dbg
);

    localparam [1:0] ST_IDLE = 2'd0,
                     ST_CTRL = 2'd1,
                     ST_DATA = 2'd2;

    reg [1:0]  st;
    reg [2:0]  cw_idx;
    reg [6:0]  g_idx;      // byte index inside the generated frame
    reg        g_src;      // 1 = data comes from the internal generator
    reg [19:0] g_tmr;      // idle timer
    reg [7:0]  cnt_svalid;
    reg [7:0]  cnt_ctready;
    reg [7:0]  cnt_gen;
    reg [7:0]  seen;

    wire c_hs = (st == ST_CTRL) && c_axis_tready;

    // ---------------- internal frame generator ----------------
    // 64 bytes: broadcast DA, SA 02:00:00:00:00:01, ethertype 0x88B5, 50 payload bytes.
    function [7:0] g_byte;
        input [6:0] i;
        begin
            case (i)
                7'd0, 7'd1, 7'd2, 7'd3, 7'd4, 7'd5:          g_byte = 8'hFF;   // DA broadcast
                7'd6:                                         g_byte = 8'h02;   // SA
                7'd7, 7'd8, 7'd9, 7'd10:                      g_byte = 8'h00;
                7'd11:                                        g_byte = 8'h01;
                7'd12:                                        g_byte = 8'h88;   // ethertype
                7'd13:                                        g_byte = 8'hB5;
                default:                                      g_byte = {1'b0, i} ^ 8'h5A;  // payload
            endcase
        end
    endfunction

    wire [7:0] g_cur = g_byte(g_idx);
    wire [1:0] g_off = g_idx[1:0];

    wire g_valid = (st == ST_DATA) && g_src;
    wire d_hs    = (st == ST_DATA) && m_axis_tready;

    // ---------------- control stream: 6 beats, tlast on the 6th ----------------
    assign c_axis_tdata  = (cw_idx == 3'd0) ? CTRL_WORD0 : 32'h0000_0000;
    assign c_axis_tkeep  = 4'hF;
    assign c_axis_tvalid = (st == ST_CTRL);
    assign c_axis_tlast  = (st == ST_CTRL) && (cw_idx == 3'd5);

    // ---------------- data stream: DMA pass-through or generator ----------------
    assign m_axis_tdata  = g_src ? ({24'h0, g_cur} << (8 * g_off)) : s_axis_tdata;
    assign m_axis_tkeep  = g_src ? (4'h1 << g_off)                 : s_axis_tkeep;
    assign m_axis_tlast  = g_src ? (g_idx == 7'd63)                : s_axis_tlast;
    assign m_axis_tvalid = (st == ST_DATA) && (g_src ? 1'b1 : s_axis_tvalid);
    assign s_axis_tready = (st == ST_DATA) && !g_src && m_axis_tready;

    always @(posedge clk) begin
        if (!rstn) begin
            st       <= ST_IDLE;
            cw_idx   <= 3'd0;
            g_idx    <= 7'd0;
            g_src    <= 1'b0;
            g_tmr    <= 20'd0;
        end else begin
            case (st)
                ST_IDLE: begin
                    if (s_axis_tvalid) begin
                        g_src  <= 1'b0;
                        cw_idx <= 3'd0;
                        st     <= ST_CTRL;
                    end else if (g_tmr >= GEN_PERIOD) begin
                        g_tmr  <= 20'd0;
                        g_src  <= 1'b1;
                        g_idx  <= 7'd0;
                        cw_idx <= 3'd0;
                        st     <= ST_CTRL;
                    end else begin
                        g_tmr <= g_tmr + 20'd1;
                    end
                end
                ST_CTRL: begin
                    if (c_hs) begin
                        if (cw_idx == 3'd5)
                            st <= ST_DATA;
                        else
                            cw_idx <= cw_idx + 3'd1;
                    end
                end
                ST_DATA: begin
                    if (g_src) begin
                        if (m_axis_tready) begin
                            if (g_idx == 7'd63) st <= ST_IDLE;
                            else                g_idx <= g_idx + 7'd1;
                        end
                    end else if (s_axis_tvalid && m_axis_tready && s_axis_tlast) begin
                        st <= ST_IDLE;
                    end
                end
                default: st <= ST_IDLE;
            endcase
        end
    end

    // ---------------- debug counters ----------------
    always @(posedge clk) begin
        if (!rstn) begin
            cnt_svalid  <= 8'd0;
            cnt_ctready <= 8'd0;
            cnt_gen     <= 8'd0;
            seen        <= 8'd0;
        end else begin
            if (s_axis_tvalid && cnt_svalid != 8'hFF)  cnt_svalid  <= cnt_svalid + 8'd1;
            if (c_axis_tready && cnt_ctready != 8'hFF) cnt_ctready <= cnt_ctready + 8'd1;
            if (s_axis_tvalid)                         seen        <= 8'hA5;
            if ((st == ST_DATA) && g_src && m_axis_tready && (g_idx == 7'd63))
                cnt_gen <= cnt_gen + 8'd1;
        end
    end

    assign dbg = {cnt_ctready, cnt_gen, cnt_svalid, seen,
                  1'b0, cw_idx, c_axis_tvalid, rstn, st, m_axis_tready, c_axis_tready, s_axis_tvalid};

endmodule
