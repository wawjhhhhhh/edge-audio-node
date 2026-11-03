//=============================================================================
// tb_fft_rt.v --- 往返测试：前向 FFT 再逆 FFT，看能不能还原输入
//
// 为什么要单独测：fft512 的"逆变换用共轭旋转因子 + 每级右移 1 位（=1/512）"
// 这条路径之前一次都没跑过，dn_top 帧级测试出现的电平亏损必须先排除它。
//=============================================================================
`timescale 1ns / 1ps

module tb_fft_rt;

    reg clk = 1'b0;
    reg rst_n = 1'b0;
    always #10 clk = ~clk;                  // 50 MHz

    reg start = 1'b0;
    reg inv = 1'b0;
    wire busy, done;

    reg               wr_en = 1'b0;
    reg [8:0]         wr_addr = 9'd0;
    reg signed [31:0] wr_re = 32'sd0;
    reg signed [31:0] wr_im = 32'sd0;

    reg [8:0] rd_addr = 9'd0;
    wire signed [31:0] rd_re, rd_im;

    fft512 #(.TW_COS("vec/tw_cos.hex"), .TW_SIN("vec/tw_sin.hex")) dut (
        .clk(clk), .rst_n(rst_n),
        .start(start), .inv(inv), .busy(busy), .done(done),
        .wr_en(wr_en), .wr_addr(wr_addr), .wr_re(wr_re), .wr_im(wr_im),
        .rd_addr(rd_addr), .rd_re(rd_re), .rd_im(rd_im)
    );

    reg [31:0] xin [0:511];
    integer i;
    integer maxerr;
    integer maxabs;
    integer e;
    integer k;
    initial begin
        $readmemh("vec/fft_in.hex", xin);
    end

    task run_fft(input integer is_inv);
        integer t;
        begin
            @(posedge clk);
            inv   <= is_inv[0];
            start <= 1'b1;
            @(posedge clk);
            start <= 1'b0;
            t = 0;
            while (!done && t < 200000) begin
                @(posedge clk);
                t = t + 1;
            end
            if (!done) begin
                $display("TIMEOUT in fft %0d", is_inv);
                $finish;
            end
        end
    endtask

    // 把 fft 内部结果读出来存到 buf，再原样写回
    reg signed [31:0] buf_re [0:511];
    reg signed [31:0] buf_im [0:511];

    task read_all;
        integer n;
        begin
            for (n = 0; n < 512; n = n + 1) begin
                rd_addr <= n[8:0];
                @(posedge clk);
                @(posedge clk);
                buf_re[n] = rd_re;
                buf_im[n] = rd_im;
            end
        end
    endtask

    task write_all;
        integer n;
        begin
            for (n = 0; n < 512; n = n + 1) begin
                wr_en   <= 1'b1;
                wr_addr <= n[8:0];
                wr_re   <= buf_re[n];
                wr_im   <= buf_im[n];
                @(posedge clk);
            end
            wr_en <= 1'b0;
        end
    endtask

    initial begin
        #100 rst_n = 1'b1;
        @(posedge clk);

        // 装载输入
        for (i = 0; i < 512; i = i + 1) begin
            wr_en   <= 1'b1;
            wr_addr <= i[8:0];
            wr_re   <= xin[i];
            wr_im   <= 32'sd0;
            @(posedge clk);
        end
        wr_en <= 1'b0;
        @(posedge clk);

        run_fft(0);            // 前向
        read_all;
        write_all;
        run_fft(1);            // 逆变换
        read_all;

        maxerr = 0; maxabs = 0;
        for (k = 0; k < 512; k = k + 1) begin
            e = $signed(buf_re[k]) - $signed(xin[k]);
            if (e < 0) e = -e;
            if (e > maxerr) maxerr = e;
            if (buf_im[k] != 0) begin
                if (buf_im[k] > 0) e = buf_im[k]; else e = -buf_im[k];
                if (e > maxerr) maxerr = e;
            end
            e = $signed(xin[k]);
            if (e < 0) e = -e;
            if (e > maxabs) maxabs = e;
        end
        $display("ROUNDTRIP maxerr=%0d maxabs=%0d rel=%.6f%%",
                 maxerr, maxabs, 100.0 * maxerr / (maxabs + 1));
        if (maxerr * 100 < maxabs) begin
            $display("RESULT: PASS");
        end else begin
            $display("RESULT: FAIL");
        end
        $finish;
    end

    initial begin
        #(5 * 1000 * 1000);
        $display("TIMEOUT");
        $finish;
    end

endmodule
