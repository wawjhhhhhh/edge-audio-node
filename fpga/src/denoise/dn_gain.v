//=============================================================================
// dn_gain.v --- 谱减的"逐 bin"处理：功率 -> 噪声跟踪 -> 比值 -> 开方 -> 平滑
//
// 与 dn_gain_model.py 逐位对齐（TB 直接比 512 个 bin 的 Y / 噪声 / 增益）。
//
// 为什么逐 bin 顺序做：512 个 bin × 约 57 拍 ≈ 29k 拍/帧，而一帧预算是
// 266k 拍（5.33 ms @ 50 MHz）——用时间换面积，省下 512 组乘法器/开方器。
//
// 定点（与设计文档 §5 一致）：
//   X 32 bit Q1.22；功率域 xr = X>>8，P = xr^2+xi^2（48 bit）
//   噪声 N 48 bit 同域；增益 Q1.15；平滑 (166*g + 90*g_prev)>>8
//=============================================================================
`timescale 1ns / 1ps

module dn_gain (
    input  wire               clk,
    input  wire               rst_n,

    input  wire               start,
    input  wire               warmup,      // 1 = 预热期：增益直通
    output reg                busy,
    output reg                done,

    // 装载频谱（自然序，一次一 bin）
    input  wire               wr_en,
    input  wire [8:0]         wr_addr,
    input  wire signed [31:0] wr_re,
    input  wire signed [31:0] wr_im,

    // 读回处理后的频谱
    input  wire [8:0]         rd_addr,
    output reg  signed [31:0] rd_re,
    output reg  signed [31:0] rd_im,

    // 读回噪声/增益状态（给 TB 与调试用）
    input  wire [8:0]         st_addr,
    output reg  [47:0]        st_n,
    output reg  [15:0]        st_g
);

    localparam [4:0] S_IDLE = 5'd0, S_RD = 5'd1, S_POW = 5'd2, S_CMP = 5'd3,
                     S_NSE  = 5'd4, S_NRM = 5'd5, S_DIV = 5'd6, S_DIVR = 5'd7,
                     S_T    = 5'd8, S_SQW = 5'd9, S_SQR = 5'd10, S_SMO = 5'd11,
                     S_MUL  = 5'd12, S_WR = 5'd13, S_RDEND = 5'd14, S_YMUL = 5'd15,
                     S_PS   = 5'd16;

    // 状态与中间量（前置声明：上面的存储块要用 wb_*）
    reg  [8:0]  bin;
    reg  [4:0]  st;
    reg         warm_r;
    reg signed [31:0] re_r, im_r;
    reg        [47:0] n_r;
    reg        [15:0] g_r;
    reg signed [23:0] xr, xi;
    reg        [47:0] p_r, n_new;
    reg        [47:0] num_r, den_r, num2_r, d24_r;
    reg        [4:0]  sh;
    reg        [23:0] q_r, t_r;
    reg        [15:0] g_new, gs_r;
    reg signed [31:0] y_re_r, y_im_r;
    reg               skip_div, skip_sqrt;

    // ---- 功率平滑（这是"降噪量上不去"的修法）-------------------------------
    // 逐 bin 单帧功率的自由度只有 2，方差极大：噪声估计 N 一旦落到 0.25P 附近，
    // 判据 P < 2.89N 就永远不成立，N 只能按 1.0005/帧 爬 → 比值恒 0.25、增益 0.6，
    // 实测只压 3~4 dB（真机与仿真一致，用户听感"跟旁路一样"）。
    // 修法：功率先做 1 bin 因果平均 + 帧间 EMA(0.5)，自由度上去之后 N 能真正收敛到 P，
    // 增益落到 floor、噪声段压 20 dB 以上。
    //   ps_c = (P[k] + P[k-1]) >> 1              只用上一个 bin，可流水
    //   ps_m = (ps_mem[k] + ps_c) >> 1           每 bin 一份帧间状态
    reg  [47:0] ps_r, ps_old, ps_c, p_prev;
    wire [47:0] ps_wd = (ps_c + ps_old) >> 1;
    wire        ps_we = (st == S_PS);

    // 写端口：wr_* 装载频谱；S_WR 那一拍直接回写处理结果（同一拍，无延迟、无残留使能）
    wire              wb_en   = (st == S_WR);
    wire [8:0]        wb_addr = bin;
    wire signed [31:0] wb_re  = y_re_r;
    wire signed [31:0] wb_im  = y_im_r;
    wire [47:0]       wb_n    = n_new;
    wire [15:0]       wb_g    = gs_r;
    // ---------------------------------------------------------------- 存储
    (* ram_style = "block" *) reg signed [31:0] sp_re [0:511];
    (* ram_style = "block" *) reg signed [31:0] sp_im [0:511];
    (* ram_style = "block" *) reg        [47:0] n_mem [0:511];
    (* ram_style = "block" *) reg        [15:0] g_mem [0:511];
    /* 影子副本：一块 BRAM 只有两个口（1 写 + 1 读），而这里每个阵列都要
       「主通路按 bin 顺序读」+「测试台按 rd_addr/st_addr 随机读」= 两个读口。
       多挂一个读口 Vivado 就不肯推断 BRAM（实测 168k LUT / 317% 器件），
       所以复制一份只读副本：写口两份一起写，BRAM 数量翻倍也就 12 个
       （器件里 140 个），完全够。 */
    (* ram_style = "block" *) reg signed [31:0] sp_re_d [0:511];
    (* ram_style = "block" *) reg signed [31:0] sp_im_d [0:511];
    (* ram_style = "block" *) reg        [47:0] n_dbg [0:511];
    (* ram_style = "block" *) reg        [15:0] g_dbg [0:511];
    (* ram_style = "block" *) reg        [47:0] ps_mem [0:511];   // 平滑功率（每 bin 一份）

    // ---------------------------------------------------------------- 清零过程
    // 原来用 initial 给阵列赋 0 —— initial + always 双驱动，Vivado 直接拒绝
    // 推断 BRAM。改成复位后用写口扫一遍 0：BRAM 上电本来就是 0，仿真里也有
    // 确定值，两边行为一致，而且是纯写操作，不影响推断。
    reg [9:0] init_cnt;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) init_cnt <= 10'd0;
        else if (init_cnt != 10'd600) init_cnt <= init_cnt + 10'd1;
    end
    wire mem_init = (init_cnt <= 10'd511);
    wire [8:0] icnt = init_cnt[8:0];

    always @(posedge clk) begin
        if (mem_init) begin
            sp_re[icnt] <= 32'sd0; sp_im[icnt] <= 32'sd0;
            sp_re_d[icnt] <= 32'sd0; sp_im_d[icnt] <= 32'sd0;
            n_mem[icnt] <= 48'd0; g_mem[icnt] <= 16'd0;
            n_dbg[icnt] <= 48'd0; g_dbg[icnt] <= 16'd0;
        end else if (wr_en && !busy) begin   // busy 期间不许外部改写（内部正在原地算）
            sp_re[wr_addr]   <= wr_re;
            sp_im[wr_addr]   <= wr_im;
            sp_re_d[wr_addr] <= wr_re;
            sp_im_d[wr_addr] <= wr_im;
        end else if (wb_en) begin
            sp_re[wb_addr]   <= wb_re;
            sp_im[wb_addr]   <= wb_im;
            sp_re_d[wb_addr] <= wb_re;
            sp_im_d[wb_addr] <= wb_im;
            n_mem[wb_addr]   <= wb_n;
            g_mem[wb_addr]   <= wb_g;
            n_dbg[wb_addr]   <= wb_n;
            g_dbg[wb_addr]   <= wb_g;
        end
    end

    // 测试台的读口只走影子副本（主通路的读在状态机里，用 bin）
    always @(posedge clk) begin
        rd_re <= sp_re_d[rd_addr];
        rd_im <= sp_im_d[rd_addr];
        st_n  <= n_dbg[st_addr];
        st_g  <= g_dbg[st_addr];
    end

    // 平滑功率：1 写（清零 / S_PS 那一拍）+ 1 读（状态机在 S_RD 读）
    always @(posedge clk) begin
        if (mem_init)      ps_mem[icnt] <= 48'd0;
        else if (ps_we)    ps_mem[bin]  <= ps_wd;
    end

    // ---------------------------------------------------------------- 子模块
    reg         div_start;
    reg  [47:0] div_n;
    reg  [23:0] div_d;
    wire [23:0] div_q;
    wire        div_done, div_busy;

    dn_div24 u_div (
        .clk(clk), .rst_n(rst_n), .start(div_start),
        .n(div_n), .d(div_d), .q(div_q), .rem(), .busy(div_busy), .done(div_done)
    );

    reg         sq_start;
    reg  [31:0] sq_x;
    wire [15:0] sq_s;
    wire        sq_done, sq_busy;

    dn_isqrt u_sqrt (
        .clk(clk), .rst_n(rst_n), .start(sq_start),
        .x(sq_x), .s(sq_s), .busy(sq_busy), .done(sq_done)
    );

    // msb：den 的最高有效位位置（组合求，48 位优先级）
    reg [5:0] msb_d;
    integer   k;
    always @* begin
        msb_d = 6'd0;
        for (k = 0; k < 48; k = k + 1)
            if (den_r[k]) msb_d = k[5:0];
    end

    wire signed [47:0] xr48 = {{24{xr[23]}}, xr};
    wire signed [47:0] xi48 = {{24{xi[23]}}, xi};
    wire        [47:0] p_w  = xr48 * xr48 + xi48 * xi48;

    wire        [63:0] n64    = {16'd0, n_r};
    wire        [63:0] p64    = {16'd0, ps_r};      // 用平滑后的功率，不再用单帧 P
    wire        [63:0] mul_thr   = (n64 * 64'd740) >> 8;
    wire        [63:0] mul_decay = (n64 * 64'd243 + p64 * 64'd13) >> 8;
    wire        [47:0] rise      = n_r + (n_r >> 11) + 48'd1;

    // 平滑与乘增益：Verilog 乘法结果宽度 = 较宽操作数，必须显式扩位
    wire        [31:0] sm_new = 32'd166 * {16'd0, g_new};
    wire        [31:0] sm_old = 32'd90  * {16'd0, g_r};
    wire        [16:0] gs_w   = (sm_new + sm_old) >> 8;
    wire signed [47:0] re48   = {{16{re_r[31]}}, re_r};
    wire signed [47:0] im48   = {{16{im_r[31]}}, im_r};
    wire signed [63:0] yre_w  = re48 * $signed({32'd0, gs_r});
    wire signed [63:0] yim_w  = im48 * $signed({32'd0, gs_r});

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st <= S_IDLE; busy <= 1'b0; done <= 1'b0;
            bin <= 9'd0; warm_r <= 1'b0;
            div_start <= 1'b0; sq_start <= 1'b0;
            n_r <= 48'd0; p_r <= 48'd0; n_new <= 48'd0;
            ps_r <= 48'd0; ps_old <= 48'd0; ps_c <= 48'd0; p_prev <= 48'd0;
            g_r <= 16'd0; g_new <= 16'd0; gs_r <= 16'd0;
            q_r <= 24'd0; t_r <= 24'd0;
            num_r <= 48'd0; den_r <= 48'd0; num2_r <= 48'd0; d24_r <= 24'd0;
            sh <= 6'd0; skip_div <= 1'b0; skip_sqrt <= 1'b0;
            y_re_r <= 32'sd0; y_im_r <= 32'sd0;
        end else begin
            done <= 1'b0;
            div_start <= 1'b0;
            sq_start  <= 1'b0;

            case (st)
                S_IDLE: begin
                    if (start && !busy) begin
                        busy   <= 1'b1;
                        bin    <= 9'd0;
                        warm_r <= warmup;
                        st     <= S_RD;
                    end
                end

                S_RD: begin
                    re_r <= sp_re[bin];
                    im_r <= sp_im[bin];
                    n_r  <= n_mem[bin];
                    g_r  <= g_mem[bin];
                    ps_old <= ps_mem[bin];
                    st   <= S_POW;
                end

                S_POW: begin
                    xr  <= re_r >>> 8;
                    xi  <= im_r >>> 8;
                    st  <= S_CMP;      // 平方要等 xr/xi 到位，下一拍再算 P
                end

                S_CMP: begin
                    p_r    <= p_w;
                    ps_c   <= (p_w + p_prev) >> 1;   // 1 bin 因果平均
                    p_prev <= p_w;
                    st     <= S_PS;
                end

                S_PS: begin
                    ps_r <= ps_wd;                   // (ps_c + ps_mem[k]) >> 1
                    st   <= S_NSE;
                end

                S_NSE: begin
                    if (warm_r) begin
                        n_new <= ps_r;                            // 预热期：噪声谱直接跟随
                    end else if (n_r == 48'd0) begin
                        n_new <= ps_r >> 2;                       // 首帧起步
                    end else if (p64 < mul_thr) begin
                        n_new <= mul_decay[47:0];                 // 安静：快速跟随
                    end else begin
                        n_new <= ({16'd0, rise} < p64) ? rise : ps_r;  // 增长夹到 P
                    end
                    st <= S_NRM;
                end

                S_NRM: begin
                    num_r <= n_new * 48'd5;
                    den_r <= ps_r * 48'd2;
                    st    <= S_T;   // 下一拍用 msb_d（组合）算位移
                end

                S_T: begin
                    // 把 den 的 MSB 对齐到 bit23：s = 23 - msb
                    if (den_r == 48'd0) begin
                        skip_div <= 1'b1;
                        q_r      <= 24'hFFFFFF;
                        st       <= S_SQW;
                    end else begin
                        if (msb_d <= 6'd23) begin
                            sh     <= 6'd23 - msb_d;
                            d24_r  <= den_r << (6'd23 - msb_d);
                            num2_r <= num_r << (6'd23 - msb_d);
                        end else begin
                            sh     <= 6'd0;
                            d24_r  <= den_r >> (msb_d - 6'd23);
                            num2_r <= num_r >> (msb_d - 6'd23);
                        end
                        st <= S_DIV;
                    end
                end

                S_DIV: begin
                    if (num2_r >= {24'd0, d24_r} || d24_r == 24'd0) begin
                        skip_div <= 1'b1;
                        q_r      <= 24'hFFFFFF;      // 比值 ≥1，全压
                        st       <= S_SQW;
                    end else begin
                        skip_div  <= 1'b0;
                        div_n     <= {24'd0, num2_r} << 24;
                        div_d     <= d24_r;
                        div_start <= 1'b1;
                        st        <= S_DIVR;
                    end
                end

                S_DIVR: begin
                    if (div_done) begin
                        q_r <= div_q;
                        st  <= S_SQW;
                    end
                end

                S_SQW: begin
                    t_r <= 24'hFFFFFF - q_r;
                    st  <= S_SQR;
                end

                S_SQR: begin
                    if (t_r == 24'd0) begin
                        g_new <= 16'd1638;           // 全压：直接给 floor，省一次开方
                        skip_sqrt <= 1'b1;
                        st <= S_SMO;
                    end else begin
                        skip_sqrt <= 1'b0;
                        sq_x      <= {2'b00, t_r, 6'b000000};   // (t<<6)，24+6=30 位
                        sq_start  <= 1'b1;
                        st        <= S_SMO;   // 等 sq_done 在 S_SMO 里等
                    end
                end

                S_SMO: begin
                    if (!skip_sqrt && !sq_done) begin
                        st <= S_SMO;                  // 继续等
                    end else begin
                        if (skip_sqrt) begin
                            g_new <= 16'd1638;
                        end else begin
                            g_new <= (sq_s < 16'd1638) ? 16'd1638 :
                                     ((sq_s > 16'd32767) ? 16'd32767 : sq_s);
                        end
                        if (warm_r)
                            g_new <= 16'd32767;
                        st <= S_MUL;
                    end
                end

                S_MUL: begin
                    gs_r <= (gs_w < 17'd1638) ? 16'd1638 : gs_w[15:0];
                    st   <= S_YMUL;      // 增益这一拍才落到 gs_r，乘要等下一拍
                end

                S_YMUL: begin
                    y_re_r <= yre_w >>> 15;   // 先在 64 位里算术右移，再截 32 位
                    y_im_r <= yim_w >>> 15;
                    st     <= S_WR;
                end

                S_WR: begin
                    if (bin == 9'd511) begin
                        busy <= 1'b0;
                        done <= 1'b1;
                        st   <= S_IDLE;
                    end else begin
                        bin <= bin + 9'd1;
                        st  <= S_RD;
                    end
                end

                default: st <= S_IDLE;
            endcase
        end
    end

endmodule
