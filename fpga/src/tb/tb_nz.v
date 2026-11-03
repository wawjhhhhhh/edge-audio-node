//=============================================================================
// tb_nz.v --- 验证 axis_dn 的底噪注入（A 方案：噪声在 PL 里、dn_top 之前）
//
// 判据：
//   1) nz=0 + bypass：输出与输入逐 beat 相同（注入不影响既有链路）
//   2) nz=3 + bypass：out-in 是零均值噪声，RMS 落在均匀 ±4096 的理论值
//      4096/sqrt(3)≈2365 的 0.5~1.5 倍（1200~3500）
//   3) nz=0 + 处理通路：不丢 beat（回归）
//
// 喂数遵从 s_axis_tready（真实 AXIS 握手）；输出由独立采集块按序存表再比对。
//=============================================================================
`timescale 1ns / 1ps

module tb_nz;

    reg clk = 0, rst_n = 0;
    always #10 clk = ~clk;              // 50 MHz

    reg         bypass = 1;
    reg  [1:0]  nz = 0;
    reg  [31:0] s_tdata = 0;
    reg         s_tvalid = 0;
    wire        s_tready;
    wire [31:0] m_tdata;
    wire        m_tvalid, m_tlast;
    wire        busy;
    wire [31:0] frames;
    wire [15:0] drop;

    axis_dn dut (
        .clk(clk), .rst_n(rst_n),
        .bypass(bypass), .nz(nz),
        .busy(busy), .frame_count(frames), .drop_cnt(drop),
        .s_axis_tdata(s_tdata), .s_axis_tvalid(s_tvalid), .s_axis_tlast(1'b0),
        .s_axis_tready(s_tready),
        .m_axis_tdata(m_tdata), .m_axis_tvalid(m_tvalid), .m_axis_tlast(m_tlast),
        .m_axis_tready(1'b1)
    );

    localparam integer NB = 3000;
    reg [15:0] gin  [0:NB-1];
    reg [15:0] gout [0:NB-1];
    integer    n_out = 0;

    // 输出采集：按到达顺序存表
    always @(posedge clk) begin
        if (m_tvalid && n_out < NB) begin
            gout[n_out] = m_tdata[15:0];
            n_out = n_out + 1;
        end
    end

    function [15:0] golden(input integer k);
        real ph;
        begin
            ph = 2.0 * 3.14159265 * 1000.0 * k / 48000.0;
            golden = $rtoi(8000.0 * $sin(ph));
        end
    endfunction

    // 一个 beat：等 tready 再给，握手完成后撤 valid
    task send(input [15:0] v);
        begin
            @(negedge clk);
            s_tvalid = 1'b1;
            s_tdata  = {v, v};
            @(posedge clk);
            while (!s_tready) @(posedge clk);   // 这一拍被接收
            @(negedge clk);
            s_tvalid = 1'b0;
            s_tdata  = 32'h0;
        end
    endtask

    integer i, err, fails;
    real    sq, rms;
    integer d, maxabs;

    initial begin
        fails = 0;
        for (i = 0; i < NB; i = i + 1) gin[i] = golden(i);
        repeat (20) @(posedge clk);
        rst_n = 1;
        repeat (10) @(posedge clk);

        // ---- 用例 1：nz=0 直通，逐 beat 一致 ----
        nz = 2'd0; bypass = 1'b1; n_out = 0;
        for (i = 0; i < NB; i = i + 1) send(gin[i]);
        repeat (50) @(posedge clk);
        err = 0;
        for (i = 0; i < NB; i = i + 1)
            if (gout[i] !== gin[i]) err = err + 1;
        $display("CASE1 nz=0 out=%0d mismatches=%0d", n_out, err);
        if (n_out != NB || err != 0) fails = fails + 1;

        // ---- 用例 2：nz=3 直通，量注入幅度 ----
        nz = 2'd3; n_out = 0;
        for (i = 0; i < NB; i = i + 1) send(gin[i]);
        repeat (50) @(posedge clk);
        sq = 0.0; maxabs = 0;
        for (i = 0; i < NB; i = i + 1) begin
            d = $signed(gout[i]) - $signed(gin[i]);
            if (d < 0) d = -d;
            if (d > maxabs) maxabs = d;
            sq = sq + d * d;
        end
        rms = $sqrt(sq / NB);
        $display("CASE2 nz=3 out=%0d rms_diff=%.1f max_diff=%0d", n_out, rms, maxabs);
        if (rms < 1200.0 || rms > 3500.0) fails = fails + 2;
        if (maxabs > 4200) fails = fails + 2;      // 不能超过 ±4096 太多

        // ---- 用例 3：nz=0 处理通路（按实时速度喂：1 beat / 1000 拍 = 48 kHz）----
        nz = 2'd0; bypass = 1'b0; n_out = 0;
        for (i = 0; i < 1280; i = i + 1) begin
            send(gin[i]);
            repeat (1000) @(posedge clk);
        end
        repeat (200000) @(posedge clk);            // 收尾：把最后一帧算完
        $display("CASE3 process in=1280 out=%0d frames=%0d drop=%0d", n_out, frames, drop);
        // 1280 点 -> 首帧 512 点，之后每 256 点一帧 -> 至少 4 帧；每帧出 256 点
        if (frames < 3) fails = fails + 4;
        if (n_out < frames * 256 - 8) fails = fails + 8;
        if (drop != 0) fails = fails + 16;

        if (fails == 0) $display("RESULT: PASS");
        else            $display("RESULT: FAIL code=%0d", fails);
        $finish;
    end

endmodule
