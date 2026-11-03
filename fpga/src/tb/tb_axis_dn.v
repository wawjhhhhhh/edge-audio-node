//=============================================================================
// tb_axis_dn.v --- axis_dn 的 AXI-Stream 测试台
//
// 用例 1  直通（bypass=1）：灌 960 beat，要求输出 960 beat、tlast 每 48 一拍、
//         数据逐 beat 一致。
// 用例 2  处理（bypass=0）：灌 3072 beat（6 帧，全在预热期内 -> 增益 1），
//         带随机反压，要求输出 = 输入的延迟版本、tlast 对齐、drop=0。
//
// 校验交给 Python：in_all*.txt / out_all*.txt 每行一个 32 bit 十六进制 beat。
//=============================================================================
`timescale 1ns / 1ps

module tb_axis_dn;

    localparam integer NBEAT1 = 960;
    localparam integer NBEAT2 = 3072;
    localparam integer DRAIN  = 400000;

    reg clk = 0, rst_n = 0;
    always #10 clk = ~clk;                  // 50 MHz

    reg         bypass = 1'b1;
    wire        busy;
    wire [31:0] frame_count;
    wire [15:0] drop_cnt;

    reg  [31:0] s_tdata  = 0;
    reg         s_tvalid = 0;
    reg         s_tlast  = 0;
    wire        s_tready;
    integer     s_sent   = 0;

    wire [31:0] m_tdata;
    wire        m_tvalid;
    wire        m_tlast;
    reg         m_tready = 1'b1;
    integer     m_got    = 0;
    integer     m_beat   = 0;
    integer     tl_bad   = 0;
    reg         backpressure = 1'b0;

    integer fd_in, fd_out;

    axis_dn dut (
        .clk(clk), .rst_n(rst_n), .bypass(bypass),
        .busy(busy), .frame_count(frame_count), .drop_cnt(drop_cnt),
        .s_axis_tdata(s_tdata), .s_axis_tvalid(s_tvalid),
        .s_axis_tlast(s_tlast), .s_axis_tready(s_tready),
        .m_axis_tdata(m_tdata), .m_axis_tvalid(m_tvalid),
        .m_axis_tlast(m_tlast), .m_axis_tready(m_tready)
    );

    // 440 Hz 正弦样点，同值填左右声道
    function [31:0] beat_at(input integer n);
        integer v;
        begin
            v = $rtoi(12000.0 * $sin(2.0 * 3.14159265358979 * 440.0 * n / 48000.0));
            beat_at = {v[15:0], v[15:0]};
        end
    endfunction

    task feed_beats(input integer n, input integer pace);
        integer i;
        begin
            for (i = 0; i < n; i = i + 1) begin
                @(negedge clk);
                s_tdata  = beat_at(i);
                s_tvalid = 1'b1;
                s_tlast  = ((i % 48) == 47);
                @(posedge clk);
                while (!s_tready) @(posedge clk);
                $fwrite(fd_in, "%08x\n", s_tdata);
                // 握手完成后在下降沿撤 valid（避免与 DUT 在同一 posedge 采样竞争，
                // 也避免 pace 期间同一个 beat 被重复收）
                @(negedge clk);
                s_tvalid = 1'b0;
                repeat (pace) @(posedge clk);   // 按真实节拍拉开（0 = 全速灌）
            end
            @(negedge clk);
            s_tvalid = 1'b0;
            s_tlast  = 1'b0;
        end
    endtask

    always @(posedge clk) if (s_tvalid && s_tready) s_sent = s_sent + 1;

    always @(posedge clk) begin
        if (m_tvalid && m_tready) begin
            $fwrite(fd_out, "%08x\n", m_tdata);
            if (m_tlast !== ((m_beat % 48) == 47)) tl_bad = tl_bad + 1;
            m_beat = m_beat + 1;
            m_got  = m_got + 1;
        end
    end

    always @(posedge clk) begin
        if (backpressure) m_tready <= (($random % 8) != 0);
        else              m_tready <= 1'b1;
    end

    initial begin : driver
        // ---------- 用例 1：直通 ----------
        fd_in  = $fopen("in_all.txt",  "w");
        fd_out = $fopen("out_all.txt", "w");
        bypass = 1'b1;
        rst_n  = 1'b0;
        repeat (20) @(posedge clk);
        rst_n  = 1'b1;
        repeat (5)  @(posedge clk);
        feed_beats(NBEAT1, 0);
        repeat (300) @(posedge clk);
        $display("CASE1 bypass: in=%0d out=%0d tl_bad=%0d drop=%0d", s_sent, m_got, tl_bad, drop_cnt);
        if (m_got != NBEAT1) $display("CASE1 FAIL: 输出 beat 数 %0d != %0d", m_got, NBEAT1);

        // ---------- 用例 2：处理 ----------
        $fclose(fd_in);  $fclose(fd_out);
        fd_in  = $fopen("in_all2.txt",  "w");
        fd_out = $fopen("out_all2.txt", "w");
        bypass       = 1'b0;
        backpressure = 1'b1;
        rst_n  = 1'b0;
        repeat (20) @(posedge clk);
        rst_n  = 1'b1;
        repeat (5)  @(posedge clk);
        s_sent = 0; m_got = 0; m_beat = 0; tl_bad = 0;
        feed_beats(NBEAT2, 400);
        repeat (DRAIN) @(posedge clk);
        backpressure = 1'b0;
        $display("CASE2 denoise: in=%0d out=%0d tl_bad=%0d drop=%0d frames=%0d",
                 s_sent, m_got, tl_bad, drop_cnt, frame_count);
        $fclose(fd_in);  $fclose(fd_out);
        $finish;
    end

    initial begin
        #200ms;
        $display("TIMEOUT: in=%0d out=%0d", s_sent, m_got);
        $finish;
    end

endmodule
