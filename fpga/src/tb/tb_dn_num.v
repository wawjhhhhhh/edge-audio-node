//=============================================================================
// tb_dn_num.v --- dn_div24 / dn_isqrt 与 Python 期望值逐项对比
//
// 判据：全部用例完全相等（这两块是纯整数运算，没有"容差"一说）。
//=============================================================================
`timescale 1ns / 1ps

module tb_dn_num;

    reg clk = 1'b0;
    reg rst_n = 1'b0;
    always #10 clk = ~clk;   // 50 MHz

    // ---------------- 除法器
    reg         dstart = 1'b0;
    reg  [47:0] dn = 48'd0;
    reg  [23:0] dd = 24'd0;
    wire [23:0] dq;
    wire [24:0] drem;
    wire        dbusy, ddone;

    dn_div24 u_div (
        .clk(clk), .rst_n(rst_n), .start(dstart),
        .n(dn), .d(dd), .q(dq), .rem(drem), .busy(dbusy), .done(ddone)
    );

    // ---------------- 开方
    reg         sstart = 1'b0;
    reg  [31:0] sx = 32'd0;
    wire [15:0] ss;
    wire        sbusy, sdone;

    dn_isqrt u_sqrt (
        .clk(clk), .rst_n(rst_n), .start(sstart),
        .x(sx), .s(ss), .busy(sbusy), .done(sdone)
    );

    integer fd, rc, i, n_div, n_sqrt, bad;
    reg [23:0] rq;
    reg [24:0] rr;
    reg [15:0] rs;
    reg [47:0] en;
    reg [23:0] ed, eq;
    reg [24:0] erem;
    reg [31:0] ex;
    reg [15:0] es;

    task run_div(input [47:0] a, input [23:0] b, output [23:0] qo, output [24:0] ro);
        begin
            @(posedge clk);
            dn <= a; dd <= b; dstart <= 1'b1;
            @(posedge clk);
            dstart <= 1'b0;
            i = 0;
            while (!ddone && i < 200) begin
                @(posedge clk);
                i = i + 1;
            end
            qo = dq;
            ro = drem;
        end
    endtask

    task run_sqrt(input [31:0] a, output [15:0] so);
        begin
            @(posedge clk);
            sx <= a; sstart <= 1'b1;
            @(posedge clk);
            sstart <= 1'b0;
            i = 0;
            while (!sdone && i < 200) begin
                @(posedge clk);
                i = i + 1;
            end
            so = ss;
        end
    endtask

    initial begin
        repeat (4) @(posedge clk);
        rst_n <= 1'b1;
        repeat (2) @(posedge clk);

        // ---------------- 除法
        fd = $fopen("vec/div_ref.txt", "r");
        if (fd == 0) begin
            $display("FATAL: cannot open vec/div_ref.txt");
            $finish;
        end
        n_div = 0;
        bad = 0;
        while (!$feof(fd)) begin
            rc = $fscanf(fd, "%h %h %h %h\n", en, ed, eq, erem);
            if (rc == 4) begin
                run_div(en, ed, rq, rr);
                n_div = n_div + 1;
                if (rq !== eq || rr !== erem) begin
                    bad = bad + 1;
                    if (bad <= 6)
                        $display("  DIV MISMATCH n=%012h d=%06h got=(%06h,%07h) exp=(%06h,%07h)",
                                 en, ed, rq, rr, eq, erem);
                end
            end
        end
        $fclose(fd);
        $display("INFO: div  %0d 用例，错 %0d", n_div, bad);
        if (bad == 0) $display("RESULT: PASS dn_div24"); else $display("RESULT: FAIL dn_div24");

        // ---------------- 开方
        fd = $fopen("vec/sqrt_ref.txt", "r");
        if (fd == 0) begin
            $display("FATAL: cannot open vec/sqrt_ref.txt");
            $finish;
        end
        n_sqrt = 0;
        bad = 0;
        while (!$feof(fd)) begin
            rc = $fscanf(fd, "%h %h\n", ex, es);
            if (rc == 2) begin
                run_sqrt(ex, rs);
                n_sqrt = n_sqrt + 1;
                if (rs !== es) begin
                    bad = bad + 1;
                    if (bad <= 6)
                        $display("  SQRT MISMATCH x=%08h got=%04h exp=%04h", ex, rs, es);
                end
            end
        end
        $fclose(fd);
        $display("INFO: sqrt %0d 用例，错 %0d", n_sqrt, bad);
        if (bad == 0) $display("RESULT: PASS dn_isqrt"); else $display("RESULT: FAIL dn_isqrt");

        $finish;
    end

    wire [23:0] dn_q;
    wire [24:0] dn_r;
    assign dn_q = dq;
    assign dn_r = drem;

    wire [15:0] sq_s;
    assign sq_s = ss;

endmodule
