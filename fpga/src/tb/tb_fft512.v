//=============================================================================
// tb_fft512.v --- FFT512 与 numpy 参考结果的对比
//
// 判据：max|dut/2^22 - ref| / max|ref| < 1%（设计文档 §7 第 3 条）。
// 只看相对误差，是因为 24 bit 旋转因子 + Q1.22 定点注定有约 -90 dB 的底噪，
// 绝对误差没有意义。
//=============================================================================
`timescale 1ns / 1ps

module tb_fft512;

    localparam N = 512;

    reg                clk = 1'b0;
    reg                rst_n = 1'b0;
    reg                start = 1'b0;
    reg                inv = 1'b0;
    reg                wr_en = 1'b0;
    reg  [8:0]         wr_addr = 9'd0;
    reg  signed [31:0] wr_re = 32'sd0;
    reg  signed [31:0] wr_im = 32'sd0;
    reg  [8:0]         rd_addr = 9'd0;
    wire signed [31:0] rd_re;
    wire signed [31:0] rd_im;
    wire               busy;
    wire               done;

    always #10 clk = ~clk;   // 50 MHz

    fft512 dut (
        .clk(clk), .rst_n(rst_n),
        .start(start), .inv(inv), .busy(busy), .done(done),
        .wr_en(wr_en), .wr_addr(wr_addr), .wr_re(wr_re), .wr_im(wr_im),
        .rd_addr(rd_addr), .rd_re(rd_re), .rd_im(rd_im)
    );

    reg signed [31:0] stim [0:N-1];
    real ref_re [0:N-1];
    real ref_im [0:N-1];
    real dut_re [0:N-1];
    real dut_im [0:N-1];

    integer i, fd, rc, cyc;
    real    a, b, max_err, max_ref, err;
    integer n_bad;

    initial begin
        $readmemh("vec/fft_in.hex", stim);

        fd = $fopen("vec/fft_ref.txt", "r");
        if (fd == 0) begin
            $display("FATAL: cannot open vec/fft_ref.txt");
            $finish;
        end
        for (i = 0; i < N; i = i + 1) begin
            rc = $fscanf(fd, "%f %f\n", a, b);
            if (rc != 2) begin
                $display("FATAL: ref line %0d parse failed rc=%0d", i, rc);
                $finish;
            end
            ref_re[i] = a;
            ref_im[i] = b;
        end
        $fclose(fd);

        repeat (4) @(posedge clk);
        rst_n <= 1'b1;
        repeat (2) @(posedge clk);

        // ---- 装载（自然序）
        for (i = 0; i < N; i = i + 1) begin
            @(posedge clk);
            wr_en   <= 1'b1;
            wr_addr <= i[8:0];
            wr_re   <= stim[i];
            wr_im   <= 32'sd0;
        end
        @(posedge clk);
        wr_en <= 1'b0;
        repeat (2) @(posedge clk);

        // ---- 启动前向 FFT
        @(posedge clk);
        start <= 1'b1;
        inv   <= 1'b0;
        @(posedge clk);
        start <= 1'b0;

        cyc = 0;
        while (!done && cyc < 200000) begin
            @(posedge clk);
            cyc = cyc + 1;
        end
        if (!done) begin
            $display("RESULT: FAIL  FFT timeout after %0d cycles", cyc);
            $finish;
        end
        $display("INFO: FFT done in %0d cycles (%.1f us @50MHz)", cyc, cyc * 0.02);

        // ---- 读回（自然序，读口 1 拍延迟）
        for (i = 0; i < N; i = i + 1) begin
            rd_addr = i[8:0];
            @(posedge clk);      // rd_re <= mem[i]
            @(posedge clk);      // rd_re 有效
            dut_re[i] = rd_re / 4194304.0;
            dut_im[i] = rd_im / 4194304.0;
        end

        // ---- 比对
        max_err = 0.0;
        max_ref = 0.0;
        n_bad   = 0;
        for (i = 0; i < N; i = i + 1) begin
            if (ref_re[i] >  max_ref) max_ref =  ref_re[i];
            if (-ref_re[i] > max_ref) max_ref = -ref_re[i];
            if (ref_im[i] >  max_ref) max_ref =  ref_im[i];
            if (-ref_im[i] > max_ref) max_ref = -ref_im[i];
        end
        for (i = 0; i < N; i = i + 1) begin
            err = dut_re[i] - ref_re[i];
            if (err < 0.0) err = -err;
            if (err > max_err) max_err = err;
            if (err > 0.01 * max_ref) n_bad = n_bad + 1;
            err = dut_im[i] - ref_im[i];
            if (err < 0.0) err = -err;
            if (err > max_err) max_err = err;
            if (err > 0.01 * max_ref) n_bad = n_bad + 1;
        end

        $display("INFO: max|X| = %.4f  max_err = %.6f  相对 = %.4f%%",
                 max_ref, max_err, 100.0 * max_err / max_ref);
        $display("INFO: bins over tolerance = %0d / %0d", n_bad, 2 * N);
        if (n_bad == 0)
            $display("RESULT: PASS  FFT512 matches numpy (rel err %.4f%%)",
                     100.0 * max_err / max_ref);
        else
            $display("RESULT: FAIL  FFT512 mismatch");

        for (i = 0; i < 6; i = i + 1)
            $display("  bin %0d: dut=(%9.3f,%9.3f)  ref=(%9.3f,%9.3f)",
                     i, dut_re[i], dut_im[i], ref_re[i], ref_im[i]);

        $finish;
    end

endmodule
