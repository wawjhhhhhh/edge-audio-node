//=============================================================================
// fft512.v --- 512 点基 2 DIT FFT / IFFT，定点 Q1.22，单蝶形器
//
// 为什么手写而不例化 xfft：整条降噪链路要跟"定点模型逐位对齐"做验证，自己有
// 数据通路才能保证 TB 里的期望值就是 numpy 的那一套；而且省掉 IP 集成到 BD 的
// 一堆约束。资源上 4 个 32x24 乘法器 + 4 块 BRAM 就够（见设计文档 §5）。
//
// 数据流：
//   1) 外部按自然序写 512 个复数（wr_addr = 自然序 n），内部写到 bitrev(n)；
//   2) start 拉高一拍，开始 9 级蝶形，就地运算（每蝶形 3 拍）；
//   3) done 拉高一拍后可自然序读出。
//
// 定点约定：
//   * 输入/中间值 32 bit Q1.22，满幅 = 2^22；
//   * 旋转因子 24 bit Q1.22，乘后右移 22；
//   * 前向不归一（增益 512，32 bit 留 1 bit 余量）；
//   * 逆变换用共轭旋转因子且每级右移 1 位，正好完成 1/512 归一。
//=============================================================================
`timescale 1ns / 1ps

module fft512 #(
    parameter TW_COS = "vec/tw_cos.hex",
    parameter TW_SIN = "vec/tw_sin.hex"
)(
    input  wire               clk,
    input  wire               rst_n,

    input  wire               start,     // 单拍脉冲；busy 时忽略
    input  wire               inv,       // start 同一拍采样：1 = 逆变换
    output reg                busy,
    output reg                done,

    input  wire               wr_en,
    input  wire [8:0]         wr_addr,
    input  wire signed [31:0] wr_re,
    input  wire signed [31:0] wr_im,

    input  wire [8:0]         rd_addr,
    output reg  signed [31:0] rd_re,
    output reg  signed [31:0] rd_im
);

    reg signed [31:0] re_mem [0:511];
    reg signed [31:0] im_mem [0:511];

    // 旋转因子改由 dn_rom.v 里的常量 ROM 提供（可综合，不依赖 .hex 文件路径），
    // 例化放在下面 tw_idx 声明之后；读的是 w_c/w_s 这两个寄存器，时序与原来一致。
    wire [23:0] rom_c;
    wire [23:0] rom_s;

    always @(posedge clk) begin
        rd_re <= re_mem[rd_addr];
        rd_im <= im_mem[rd_addr];
    end

    // ---------------------------------------------------------------- 计数器
    reg [3:0] stage;
    reg [7:0] blk;
    reg [8:0] jj;
    reg [1:0] ph;
    reg       inv_r;

    wire [8:0] half   = 9'd1 << stage;
    wire [8:0] i_a    = (blk << (stage + 1)) | jj;
    wire [8:0] i_b    = i_a + half;
    wire [8:0] tw_idx = {1'b0, (jj << (8 - stage))};   // 低 8 位有效，补零到 9 位接 ROM
    wire       last_blk = (blk == (9'd511 >> (stage + 1)));
    wire       last_j   = (jj == (half - 9'd1));

    dn_tw_rom u_tw (
        .addr(tw_idx),
        .c(rom_c),
        .s(rom_s)
    );

    // ---------------------------------------------------------------- 蝶形
    reg signed [31:0] a_re, a_im, b_re, b_im;
    reg signed [23:0] w_c, w_s;

    wire signed [23:0] w_s_eff = inv_r ? -w_s : w_s;

    wire signed [55:0] p_rr = b_re * w_c;
    wire signed [55:0] p_ii = b_im * w_s_eff;
    wire signed [55:0] p_ri = b_re * w_s_eff;
    wire signed [55:0] p_ir = b_im * w_c;

    wire signed [31:0] t_re = (p_rr - p_ii) >>> 22;
    wire signed [31:0] t_im = (p_ri + p_ir) >>> 22;

    wire signed [31:0] sum_re = a_re + t_re;
    wire signed [31:0] sum_im = a_im + t_im;
    wire signed [31:0] dif_re = a_re - t_re;
    wire signed [31:0] dif_im = a_im - t_im;

    wire signed [31:0] ra_re = inv_r ? (sum_re >>> 1) : sum_re;
    wire signed [31:0] ra_im = inv_r ? (sum_im >>> 1) : sum_im;
    wire signed [31:0] rb_re = inv_r ? (dif_re >>> 1) : dif_re;
    wire signed [31:0] rb_im = inv_r ? (dif_im >>> 1) : dif_im;

    // ---------------------------------------------------------------- 主 FSM
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            busy  <= 1'b0;
            done  <= 1'b0;
            stage <= 4'd0;
            blk   <= 8'd0;
            jj    <= 9'd0;
            ph    <= 2'd0;
            inv_r <= 1'b0;
        end else begin
            done <= 1'b0;

            if (start && !busy) begin
                busy  <= 1'b1;
                inv_r <= inv;
                stage <= 4'd0;
                blk   <= 8'd0;
                jj    <= 9'd0;
                ph    <= 2'd0;
            end else if (busy) begin
                case (ph)
                    2'd0: begin
                        a_re <= re_mem[i_a];
                        a_im <= im_mem[i_a];
                        b_re <= re_mem[i_b];
                        b_im <= im_mem[i_b];
                        w_c  <= rom_c;
                        w_s  <= rom_s;
                        ph   <= 2'd1;
                    end
                    2'd1: begin
                        ph <= 2'd2;
                    end
                    default: begin
                        ph <= 2'd0;
                        if (last_j) begin
                            jj <= 9'd0;
                            if (last_blk) begin
                                blk <= 8'd0;
                                if (stage == 4'd8) begin
                                    busy <= 1'b0;
                                    done <= 1'b1;
                                end else begin
                                    stage <= stage + 4'd1;
                                end
                            end else begin
                                blk <= blk + 8'd1;
                            end
                        end else begin
                            jj <= jj + 9'd1;
                        end
                    end
                endcase
            end
        end
    end

    // 写入端口：自然序 -> bit-reversed 地址（512 点位反转，纯连线）
    wire [8:0] wr_brev = {wr_addr[0], wr_addr[1], wr_addr[2], wr_addr[3], wr_addr[4],
                          wr_addr[5], wr_addr[6], wr_addr[7], wr_addr[8]};
    // 注意：变换进行中（busy）必须挡住写入 —— 蝶形是原地计算，
    // 若 wr_en 一直挂着，会把某一级已经算好的结果按同一个地址反复盖掉，
    // 变换结果就被"打散"（能量守恒但内容是乱的）。
    // 存储器的唯一写口：外部装载 / 蝶形写回都在这里选，绝不分两个 always 块
    // （分两块 = 两个写口，Vivado 报 "Invalid write to RAM"，整个 FFT 退化成
    //   512x32 触发器 + 巨型多路选择器）。
    // 初始化：复位后 512 拍把整片写 0。BRAM 上电本来就是 0，但仿真里是 X，
    // 有个显式清零过程两边行为才一致；同时它走写口，不影响 BRAM 推断。
    reg [9:0] init_cnt;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) init_cnt <= 10'd0;
        else if (init_cnt != 10'd600) init_cnt <= init_cnt + 10'd1;
    end
    wire mem_init = (init_cnt <= 10'd511);

    always @(posedge clk) begin
        if (mem_init) begin
            re_mem[init_cnt[8:0]] <= 32'sd0;
            im_mem[init_cnt[8:0]] <= 32'sd0;
        end else if (wr_en && !busy) begin
            re_mem[wr_brev] <= wr_re;
            im_mem[wr_brev] <= wr_im;
        end else if (busy) begin
            if (ph == 2'd1) begin
                re_mem[i_a] <= ra_re;
                im_mem[i_a] <= ra_im;
            end else if (ph == 2'd2) begin
                re_mem[i_b] <= rb_re;
                im_mem[i_b] <= rb_im;
            end
        end
    end

endmodule
