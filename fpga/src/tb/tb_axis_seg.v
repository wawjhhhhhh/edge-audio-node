//=============================================================================
// tb_axis_seg.v --- 用真实的 AXIS 通路跑 A/B/C/D 四段信号，量"降噪压了多少 dB"
//
// 为什么不用 tb_dn_top：那条 TB 直接怼 dn_top 的 16 bit 口、喂数节奏和真实
// 通路不同，修复 hist 索引越界之后它的分段电平表出现 +12 dB / 削波，与同一条
// RTL 在 AXIS 通路下的表现矛盾。这里从 axis_dn 的从端口灌、主端口收，
// 走的就是上板时的那条路。
//
// 信号：vec/top_in.hex（gen_top_pcm.py 生成，79360 点）
//   A   0 ..33280   纯噪声（让噪声估计收敛）
//   B   33280..48640 纯噪声（量压制）
//   C   48640..64000 语音+噪声
//   D   64000..79360 纯语音
//=============================================================================
`timescale 1ns / 1ps

module tb_axis_seg;

    localparam integer NPCM  = 79360;
    localparam integer DRAIN = 4000000;

    reg clk = 0, rst_n = 0;
    always #10 clk = ~clk;                  // 50 MHz

    reg         bypass = 1'b0;
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
    integer     tl_bad   = 0;

    integer pcm [0:NPCM-1];
    integer fd, i;

    // 四段累加（64 位避免溢出）
    reg signed [63:0] out_sum [0:3];
    reg signed [63:0] in_sum  [0:3];
    integer out_n [0:3];
    integer in_n  [0:3];

    axis_dn dut (
        .clk(clk), .rst_n(rst_n), .bypass(bypass),
        .busy(busy), .frame_count(frame_count), .drop_cnt(drop_cnt),
        .s_axis_tdata(s_tdata), .s_axis_tvalid(s_tvalid),
        .s_axis_tlast(s_tlast), .s_axis_tready(s_tready),
        .m_axis_tdata(m_tdata), .m_axis_tvalid(m_tvalid),
        .m_axis_tlast(m_tlast), .m_axis_tready(m_tready)
    );

    function integer seg_of(input integer n);
        begin
            if (n < 33280)      seg_of = 0;
            else if (n < 48640) seg_of = 1;
            else if (n < 64000) seg_of = 2;
            else                seg_of = 3;
        end
    endfunction

    // 输出侧：取低 16 位（左声道）累加
    reg signed [15:0] ov;
    integer           oseg;
    always @(posedge clk) begin
        if (m_tvalid && m_tready) begin
            ov   = m_tdata[15:0];
            oseg = seg_of(m_got);
            out_sum[oseg] = out_sum[oseg] + ov * ov;
            out_n[oseg]   = out_n[oseg] + 1;
            if (m_tlast !== ((m_got % 48) == 47)) tl_bad = tl_bad + 1;
            m_got = m_got + 1;
        end
    end

    task feed_all(input integer pace);
        integer n;
        begin
            for (n = 0; n < NPCM; n = n + 1) begin
                @(negedge clk);
                s_tdata  = {pcm[n][15:0], pcm[n][15:0]};
                s_tvalid = 1'b1;
                s_tlast  = ((n % 48) == 47);
                @(posedge clk);
                while (!s_tready) @(posedge clk);
                s_sent = s_sent + 1;
                @(negedge clk);
                s_tvalid = 1'b0;
                repeat (pace) @(posedge clk);
            end
            @(negedge clk);
            s_tvalid = 1'b0;
            s_tlast  = 1'b0;
        end
    endtask

    initial begin : driver
        for (i = 0; i < 4; i = i + 1) begin
            out_sum[i] = 0; in_sum[i] = 0; out_n[i] = 0; in_n[i] = 0;
        end
        fd = $fopen("top_in.hex", "r");
        if (fd == 0) begin
            $display("SEG FAIL: cannot open top_in.hex");
            $finish;
        end
        for (i = 0; i < NPCM; i = i + 1) begin
            void'($fscanf(fd, "%d", pcm[i]));
            in_sum[seg_of(i)] = in_sum[seg_of(i)] + pcm[i] * pcm[i];
            in_n[seg_of(i)]   = in_n[seg_of(i)] + 1;
        end
        $fclose(fd);

        rst_n = 1'b0;
        repeat (20) @(posedge clk);
        rst_n = 1'b1;
        repeat (5)  @(posedge clk);

        feed_all(250);                    // 250 拍/样点 ≈ 48 kHz 的量级
        repeat (DRAIN) @(posedge clk);

        $display("SEG DONE in=%0d out=%0d tl_bad=%0d drop=%0d frames=%0d",
                 s_sent, m_got, tl_bad, drop_cnt, frame_count);
        for (i = 0; i < 4; i = i + 1)
            $display("SEG %0d in_sum=%0d in_n=%0d out_sum=%0d out_n=%0d",
                     i, in_sum[i], in_n[i], out_sum[i], out_n[i]);
        $finish;
    end

    initial begin
        #600000000;
        $display("TIMEOUT: in=%0d out=%0d", s_sent, m_got);
        $finish;
    end

endmodule
