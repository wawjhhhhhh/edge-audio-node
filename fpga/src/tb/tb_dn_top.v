//=============================================================================
// tb_dn_top.v --- dn_top 帧级验证：喂 200 帧"语音 + 宽带噪声"，按段统计电平
//
// 信号由 gen_top_pcm.py 生成（vec/top_in.hex，每行一个 16 bit 样点）：
//   A  帧   0.. 19  语音+噪声（预热期，60 帧内）
//   B  帧  20.. 59  语音+噪声（预热结束）
//   C  帧  60.. 89  纯噪声（关键指标：压了多少 dB）
//   D  帧  90..119  语音（电平有没有跑偏）
//
// TB 只负责把输入按 48 kHz 灌进去、收集输出，并按段累加"平方和 + 样点数"，
// 最后打印出来，dB 与 PASS/FAIL 由 .tmp/sim_top.ps1 之后的 Python 判。
//=============================================================================
`timescale 1ns / 1ps

module tb_dn_top;

    localparam integer NSAMP = 79360;      // 310 帧 × 256 点
    localparam integer HOP   = 256;

    reg clk = 1'b0;
    reg rst_n = 1'b0;
    reg in_valid = 1'b0;
    reg signed [15:0] in_l = 16'sd0;
    wire out_valid;
    wire signed [15:0] out_l;
    wire [31:0] frame_count;
    wire busy;

    dn_top #(
        .TW_COS("vec/tw_cos.hex"),
        .TW_SIN("vec/tw_sin.hex"),
        .WIN("vec/win.hex"),
        .WARMUP(60)
    ) dut (
        .clk(clk), .rst_n(rst_n), .bypass(1'b0),
        .in_valid(in_valid), .in_l(in_l),
        .out_valid(out_valid), .out_l(out_l),
        .frame_count(frame_count), .busy(busy)
    );

    always #10 clk = ~clk;                  // 50 MHz

    // ---------------------------------------------------------------- 输入向量
    integer pcm [0:NSAMP-1];
    integer i;
    integer fd;
    integer rc;
    initial begin
        for (i = 0; i < NSAMP; i = i + 1) pcm[i] = 0;
        fd = $fopen("vec/top_in.hex", "r");
        if (fd == 0) begin
            $display("CANNOT OPEN vec/top_in.hex");
            $finish;
        end
        i = 0;
        while (!$feof(fd) && i < NSAMP) begin
            rc = $fscanf(fd, "%d", pcm[i]);
            if (rc == 1) i = i + 1;
        end
        $fclose(fd);
        $display("LOADED %0d samples", i);
    end

    // ---------------------------------------------------------------- 统计
    // 段边界（按样点）
    //   A: 0 .. 5119      B: 5310 .. 15359
    //   C: 15360 .. 38399 D: 38400 .. 51199
    real   sum_in [0:3];
    real   sum_out[0:3];
    integer n_in  [0:3];
    integer n_out [0:3];
    integer xerr;
    integer out_count;

    function integer seg_of(input integer idx);
        begin
            if (idx < 130 * HOP)      seg_of = 0;
            else if (idx < 190 * HOP) seg_of = 1;
            else if (idx < 250 * HOP) seg_of = 2;
            else                      seg_of = 3;
        end
    endfunction

    initial begin
        for (i = 0; i < 4; i = i + 1) begin
            sum_in[i] = 0.0; sum_out[i] = 0.0; n_in[i] = 0; n_out[i] = 0;
        end
        xerr = 0; out_count = 0;
    end

    // 全量输出转储：给 Python 侧复核分段 dB（TB 自己只算 rms，不做判定）
    integer fd_all;
    initial fd_all = $fopen("out_all.txt", "w");

    // 输出统计
    integer so;
    always @(posedge clk) begin
        if (out_valid) begin
            if (^out_l === 1'bx) xerr = xerr + 1;
            if (out_count < NSAMP) begin
                so = seg_of(out_count);
                sum_out[so] = sum_out[so] + $itor(out_l) * $itor(out_l);
                n_out[so]  = n_out[so] + 1;
            end
            $fwrite(fd_all, "%0d\n", out_l);
            out_count  = out_count + 1;
        end
    end

    // ---------------------------------------------------------------- 灌样点
    // 48 kHz：每 1042 个 50 MHz 周期送一个样点（1041.67）
    integer ci;
    integer idx;
    initial begin
        idx = 0;
        @(posedge rst_n);
        forever begin
            for (ci = 0; ci < 250; ci = ci + 1) @(posedge clk);
            if (idx < NSAMP + 256) begin
                in_valid <= 1'b1;
                in_l     <= (idx < NSAMP) ? pcm[idx][15:0] : 16'sd0;
                if (idx < NSAMP) begin
                    sum_in[seg_of(idx)] = sum_in[seg_of(idx)] + $itor($signed(pcm[idx])) * $itor($signed(pcm[idx]));
                    n_in[seg_of(idx)]   = n_in[seg_of(idx)] + 1;
                end
                @(posedge clk);
                in_valid <= 1'b0;
                idx = idx + 1;
            end
        end
    end

    // ---------------------------------------------------------------- 结束
    initial begin
        #100 rst_n = 1'b1;
        wait (out_count >= NSAMP);
        #(250 * 40);                         // 等最后输出排空
        $display("DONE frames=%0d out_count=%0d xerr=%0d", frame_count, out_count, xerr);
        for (i = 0; i < 4; i = i + 1) begin
            $display("SEG %0d in_sum=%f in_n=%0d out_sum=%f out_n=%0d",
                     i, sum_in[i], n_in[i], sum_out[i], n_out[i]);
        end
        $finish;
    end

    initial begin
        repeat (4) #(1000 * 1000 * 1000);  // 4 s 仿真时间上限（分四段，避开 32 位整数溢出）
        $display("TIMEOUT frames=%0d out_count=%0d", frame_count, out_count);
        $finish;
    end

endmodule
