//=============================================================================
// afifo.v --- First-Word-Fall-Through asynchronous FIFO (gray pointers)
//
// Data width DW, depth 2**AW.  FWFT means rdata always shows the head word,
// so a consumer can decide to pop or not based on rempty.
//
// Write side : wclk / wrst_n / wpush / wdata / wfull
// Read  side : rclk / rrst_n / rpop  / rdata / rempty
//
// NOTE on the full/empty computation (this bit is easy to get wrong):
//   wfull / rempty are computed from the CURRENT gray pointer compared against
//   the synchronised pointer of the other side.  Do NOT feed wfull back into
//   the next-pointer arithmetic (`wbin + (wpush & ~wfull)`) while also deriving
//   wfull from that next pointer -- that is a combinational loop and Vivado
//   reports it as DRC LUTLP-1 (7 LUTs in a loop through u_rx_fifo).
//   The pointer is advanced only when the transfer actually happens, so the
//   current-pointer comparison is already exact.
//=============================================================================
module afifo #(
    parameter DW = 32,
    parameter AW = 4
)(
    input  wire          wclk,
    input  wire          wrst_n,
    input  wire          wpush,
    input  wire [DW-1:0] wdata,
    output wire          wfull,

    input  wire          rclk,
    input  wire          rrst_n,
    input  wire          rpop,
    output wire [DW-1:0] rdata,
    output wire          rempty
);

    reg [DW-1:0] mem [0:(1<<AW)-1];

    reg  [AW:0] wbin, wgray;
    reg  [AW:0] rbin, rgray;

    // gray pointers crossed into the opposite domain
    (* ASYNC_REG = "TRUE" *) reg [AW:0] wq1, wq2;
    (* ASYNC_REG = "TRUE" *) reg [AW:0] rq1, rq2;

    // ---------------------------------------------------------------- write
    wire [AW:0] wbin_nxt  = wbin + 1'b1;
    wire [AW:0] wgray_nxt = (wbin_nxt >> 1) ^ wbin_nxt;

    // full when the write pointer has caught up with the read pointer, one
    // wrap apart: same low AW-1 bits, top two bits inverted
    assign wfull = (wgray == {~rq2[AW:AW-1], rq2[AW-2:0]});

    wire winc = wpush & ~wfull;

    always @(posedge wclk or negedge wrst_n) begin
        if (!wrst_n) begin
            wbin  <= {(AW+1){1'b0}};
            wgray <= {(AW+1){1'b0}};
        end else if (winc) begin
            wbin  <= wbin_nxt;
            wgray <= wgray_nxt;
            mem[wbin[AW-1:0]] <= wdata;
        end
    end

    always @(posedge wclk or negedge wrst_n) begin
        if (!wrst_n) {rq2, rq1} <= {(2*(AW+1)){1'b0}};
        else         {rq2, rq1} <= {rq1, rgray};
    end

    // ----------------------------------------------------------------- read
    wire [AW:0] rbin_nxt  = rbin + 1'b1;
    wire [AW:0] rgray_nxt = (rbin_nxt >> 1) ^ rbin_nxt;

    assign rempty = (rgray == wq2);
    assign rdata  = mem[rbin[AW-1:0]];

    wire rinc = rpop & ~rempty;

    always @(posedge rclk or negedge rrst_n) begin
        if (!rrst_n) begin
            rbin  <= {(AW+1){1'b0}};
            rgray <= {(AW+1){1'b0}};
        end else if (rinc) begin
            rbin  <= rbin_nxt;
            rgray <= rgray_nxt;
        end
    end

    always @(posedge rclk or negedge rrst_n) begin
        if (!rrst_n) {wq2, wq1} <= {(2*(AW+1)){1'b0}};
        else         {wq2, wq1} <= {wq1, wgray};
    end

endmodule
