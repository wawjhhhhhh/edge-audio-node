//=============================================================================
// tb_dn_gain.v --- 逐位比对 dn_gain 与 dn_gain_model.py
//
// 输入：vec/gain_stim_f<f>.hex   每行 "re im"（8 位十六进制补码）
// 期望：vec/gain_ref_f<f>.txt    每行 "yre yim n g"
//
// 三帧依次喂入（噪声状态跨帧累积），每帧比 512 个 bin 的 Y / N / G。
//=============================================================================
`timescale 1ns / 1ps

module tb_dn_gain;

    reg clk = 1'b0;
    reg rst_n = 1'b0;
    always #10 clk = ~clk;

    reg               start = 1'b0;
    reg               warmup = 1'b0;
    wire              busy, done;

    reg               wr_en = 1'b0;
    reg [8:0]         wr_addr = 9'd0;
    reg signed [31:0] wr_re = 32'sd0, wr_im = 32'sd0;

    reg [8:0]          rd_addr = 9'd0;
    wire signed [31:0] rd_re, rd_im;
    reg [8:0]          st_addr = 9'd0;
    wire [47:0]        st_n;
    wire [15:0]        st_g;

    dn_gain dut (
        .clk(clk), .rst_n(rst_n),
        .start(start), .warmup(warmup), .busy(busy), .done(done),
        .wr_en(wr_en), .wr_addr(wr_addr), .wr_re(wr_re), .wr_im(wr_im),
        .rd_addr(rd_addr), .rd_re(rd_re), .rd_im(rd_im),
        .st_addr(st_addr), .st_n(st_n), .st_g(st_g)
    );

    reg [31:0] stim [0:1023];
    integer    fd, i, f, errs, cyc, nread;
    reg [31:0] e_re, e_im;
    reg [47:0] e_n;
    reg [15:0] e_g;

    task load_frame(input integer idx);
        begin
            $readmemh($sformatf("vec/gain_stim_f%0d.hex", idx), stim);
            for (i = 0; i < 512; i = i + 1) begin
                @(negedge clk);
                wr_en   <= 1'b1;
                wr_addr <= i[8:0];
                wr_re   <= stim[2*i];
                wr_im   <= stim[2*i+1];
            end
            @(negedge clk);
            wr_en <= 1'b0;
        end
    endtask

    task run_one(input integer idx);
        begin
            load_frame(idx);

            @(negedge clk);
            start <= 1'b1;
            @(negedge clk);
            start <= 1'b0;

            cyc = 0;
            while (!done && cyc < 400000) begin
                @(posedge clk);
                cyc = cyc + 1;
            end
            if (!done) begin
                $display("  frame %0d FAIL: no done (%0d cycles)", idx, cyc);
                errs = errs + 1;
            end else begin
                $display("  frame %0d: done in %0d cycles", idx, cyc);
            end

            fd = $fopen($sformatf("vec/gain_ref_f%0d.txt", idx), "r");
            if (fd == 0) begin
                $display("  frame %0d FAIL: cannot open ref", idx);
                errs = errs + 1;
            end else begin
                for (i = 0; i < 512; i = i + 1) begin
                    nread = $fscanf(fd, "%h %h %h %h\n", e_re, e_im, e_n, e_g);
                    rd_addr <= i[8:0];
                    st_addr <= i[8:0];
                    @(posedge clk);
                    @(posedge clk);
                    @(posedge clk);   // 读地址打进寄存器 -> 再取一拍才是数据
                    #1;
                    if (nread != 4) begin
                        $display("  bin %0d: ref parse failed", i);
                        errs = errs + 1;
                    end else begin
                        if (rd_re !== $signed(e_re) || rd_im !== $signed(e_im)) begin
                            if (errs < 6)
                                $display("  bin %0d Y: got (%0d,%0d) exp (%0d,%0d)",
                                         i, rd_re, rd_im, $signed(e_re), $signed(e_im));
                            errs = errs + 1;
                        end
                        if (st_n !== e_n || st_g !== e_g) begin
                            if (errs < 6)
                                $display("  bin %0d N/G: got (%h,%h) exp (%h,%h)",
                                         i, st_n, st_g, e_n, e_g);
                            errs = errs + 1;
                        end
                    end
                end
                $fclose(fd);
            end
            repeat (5) @(posedge clk);
        end
    endtask

    initial begin
        errs = 0;
        $display("=== dn_gain bit-exact TB ===");
        repeat (5) @(posedge clk);
        rst_n = 1'b1;
        // 等内部存储器清零过程结束（init_cnt 数到 511 共 512 拍，留余量到 620）
        // 不等的话装载会被清零分支吃掉 —— 表现为"第 0 帧全是 0"。
        repeat (620) @(posedge clk);

        for (f = 0; f < 3; f = f + 1)
            run_one(f);

        if (errs == 0) $display("RESULT: PASS  (3 frames x 512 bins bit-exact)");
        else           $display("RESULT: FAIL  (%0d mismatches)", errs);
        $finish;
    end

    initial begin
        #40_000_000;
        $display("RESULT: FAIL (timeout)");
        $finish;
    end

endmodule
