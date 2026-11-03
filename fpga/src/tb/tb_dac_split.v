/* tb_dac_split.v -- 双 DAC 门控的逐位验证
 *
 * 做法：造一段 64 bit 的 I2S 帧（左 32 bit + 右 32 bit，已知模式），
 * 对每种 mode 把 dout_a / dout_b 逐 bit 收回来比对期望。
 * 判据：M0 两口都等于原流；M1 板载只有左时隙、外接只有右时隙；
 *       M2 板载全 0、外接等于原流。
 *
 * 编译：xvlog -sv dac_split.v tb_dac_split.v ; xelab tb_dac_split -s dac ; xsim dac -runall
 */
`timescale 1ns / 1ps

module tb_dac_split;

    reg       din = 1'b0;
    reg       lrc = 1'b0;
    reg [1:0] mode = 2'd0;

    wire dout_a, dout_b;

    dac_split dut (.din(din), .lrc(lrc), .mode(mode), .dout_a(dout_a), .dout_b(dout_b));

    /* 一帧 64 bit：左时隙用 0xA5A5A5A5，右时隙用 0x5A5A5A5A，MSB 先出 */
    reg  [63:0] frame;

    integer i;
    integer err_a, err_b;
    integer checked;

    /* 期望值计算：给定 mode，返回第 i bit 上 A/B 应为什么 */
    function exp_a(input [1:0] m, input [63:0] f, input integer idx);
        begin
            if (m == 2'd2) exp_a = 1'b0;
            else if (m == 2'd1) exp_a = (idx < 32) ? f[63 - idx] : 1'b0;
            else                exp_a = f[63 - idx];
        end
    endfunction

    function exp_b(input [1:0] m, input [63:0] f, input integer idx);
        begin
            if (m == 2'd1) exp_b = (idx >= 32) ? f[63 - idx] : 1'b0;
            else           exp_b = f[63 - idx];
        end
    endfunction

    task run_mode(input [1:0] m);
        begin
            mode = m;
            err_a = 0; err_b = 0; checked = 0;
            for (i = 0; i < 64; i = i + 1) begin
                din = frame[63 - i];
                lrc = (i < 32) ? 1'b0 : 1'b1;   /* 左 32 bit LRC=0，右 32 bit LRC=1 */
                #10;
                if (dout_a !== exp_a(m, frame, i)) err_a = err_a + 1;
                if (dout_b !== exp_b(m, frame, i)) err_b = err_b + 1;
                checked = checked + 1;
            end
            $display("MODE %0d  bits=%0d  err_a=%0d  err_b=%0d", m, checked, err_a, err_b);
        end
    endtask

    initial begin
        frame = {32'hA5A5A5A5, 32'h5A5A5A5A};

        run_mode(2'd0);
        run_mode(2'd1);
        run_mode(2'd2);
        run_mode(2'd3);

        if ((err_a == 0) && (err_b == 0))
            $display("RESULT: PASS (all 4 modes bit-exact)");
        else
            $display("RESULT: FAIL");

        /* 补一条：M0 时两口必须**同相**（同一时刻完全一样），否则双口 A/B 无意义 */
        mode = 2'd0;
        for (i = 0; i < 64; i = i + 1) begin
            din = frame[63 - i];
            lrc = (i < 32) ? 1'b0 : 1'b1;
            #10;
            if (dout_a !== dout_b) begin
                $display("RESULT: FAIL (M0 两口不同相, bit %0d)", i);
                $finish;
            end
        end
        $display("M0 两口逐位同相 OK");
        $finish;
    end

endmodule
