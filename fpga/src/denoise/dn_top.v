//=============================================================================
// dn_top.v --- 谱减降噪顶层：开窗 → 前向 FFT → 增益 → 逆 FFT → 合成窗 → 重叠相加
//
// 接口按"样点流"设计（48 kHz，每 256 点算一帧，帧长 512、重叠 50%），
// 便于以后包成 AXI-Stream 挂到 audio_axis_core 后面。
//
//   in_valid/in_l : 16 bit 有符号样点，进来先转 Q1.22（<<7）
//   out_valid/out_l : 同样 16 bit，处理完按帧吐出 256 点
//   bypass        : 1 = 直通（不经过 FFT/增益，只做 256 点延迟对齐）
//
// 帧内时序（50 MHz）：装载 1024 + 前向 FFT 6913 + 拷贝 512 + 增益 28673
// + 拷贝 1024 + 逆 FFT 6913 + 合成 1024 + 输出 256 ≈ 44.4k 拍 = 0.89 ms
// 远小于帧移 5.33 ms，余量 6 倍。
//=============================================================================
`timescale 1ns / 1ps

module dn_top #(
    parameter TW_COS = "vec/tw_cos.hex",
    parameter TW_SIN = "vec/tw_sin.hex",
    parameter WIN    = "vec/win.hex",
    parameter WARMUP = 60,                // 预热帧数：这段时间增益直通
    parameter SKIP_GAIN = 0               // 仅仿真用：1 = 跳过谱减，验证窗+FFT+OLA 的增益
)(
    input  wire               clk,
    input  wire               rst_n,
    input  wire               bypass,

    input  wire               in_valid,
    input  wire signed [15:0] in_l,
    output reg                out_valid,
    output reg  signed [15:0] out_l,

    output reg  [31:0]        frame_count,
    output wire               busy
);

    localparam [3:0] S_IDLE   = 4'd0,
                     S_SMP    = 4'd1,   // 把 512 点搬进 FFT（乘分析窗）
                     S_FFT    = 4'd2,   // 前向 FFT
                     S_CPIN   = 4'd3,   // 频谱 -> 增益模块
                     S_GAIN   = 4'd4,
                     S_CPBK   = 4'd5,   // 增益结果 -> FFT（准备逆变换）
                     S_IFFT   = 4'd6,
                     S_SYN    = 4'd7,   // 乘合成窗 + 重叠相加
                     S_OUT    = 4'd8;   // 吐 256 点

    reg [3:0]  st;
    reg [9:0]  k;                 // 0..511
    reg [8:0]  k9;
    reg [1:0]  ph;                // 子相位（读/写两拍）
    reg [9:0]  fb;                // 本帧起点（开帧时锁存，避免处理期间 wptr 前移）

    // ---------------------------------------------------------------- 样点缓冲
    (* ram_style = "block" *) reg signed [15:0] hist [0:511];
    reg [8:0]         wptr;       // 写入位置（每帧 +256，只需 9 位指向 512 深的 hist）
    reg [9:0]         acc;        // 已攒样点数

    // ---------------------------------------------------------------- 窗 & OLA
    // 窗改由 dn_rom.v 的常量 ROM 提供（可综合，不依赖 .hex 路径）；组合读出，
    // 用法与原 reg 数组完全相同。
    wire [15:0]      win_q;
    (* ram_style = "block" *) reg signed [31:0] ola  [0:511];
    dn_win_rom u_win (
        .addr(k[8:0]),
        .q(win_q)
    );
    wire signed [15:0] win_v = win_q;

    // ola 的清零过程：原来用 initial 赋 0，initial + always 双驱动会让 Vivado
    // 拒绝把 ola 推断成 BRAM（整个阵列退化成触发器）。改成复位后 512 拍用
    // 写口扫 0 —— 第一帧要等 hist 攒满 512 点（≈14.7 ms）才开，而清零只要
    // 10 µs，两者不冲突。
    // hist 不需要清零：开帧条件就是「512 点全部写过了」，等于自己写满一遍。
    reg [9:0] init_cnt;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) init_cnt <= 10'd0;
        else if (init_cnt != 10'd600) init_cnt <= init_cnt + 10'd1;
    end
    wire mem_init = (init_cnt <= 10'd511);
    wire [8:0] icnt = init_cnt[8:0];

    // ---------------------------------------------------------------- FFT
    reg               fft_start, fft_inv;
    wire              fft_busy, fft_done;
    reg               fwr_en;
    reg  [8:0]        fwr_addr;
    reg  signed [31:0] fwr_re, fwr_im;
    reg  [8:0]        frd_addr;
    wire signed [31:0] frd_re, frd_im;

    fft512 #(.TW_COS(TW_COS), .TW_SIN(TW_SIN)) u_fft (
        .clk(clk), .rst_n(rst_n),
        .start(fft_start), .inv(fft_inv), .busy(fft_busy), .done(fft_done),
        .wr_en(fwr_en), .wr_addr(fwr_addr), .wr_re(fwr_re), .wr_im(fwr_im),
        .rd_addr(frd_addr), .rd_re(frd_re), .rd_im(frd_im)
    );

    // ---------------------------------------------------------------- 增益
    reg               g_start, g_warmup;
    wire              g_busy, g_done;
    reg               gwr_en;
    reg  [8:0]        gwr_addr;
    reg  signed [31:0] gwr_re, gwr_im;
    reg  [8:0]        grd_addr;
    wire signed [31:0] grd_re, grd_im;

    dn_gain u_gain (
        .clk(clk), .rst_n(rst_n),
        .start(g_start), .warmup(g_warmup), .busy(g_busy), .done(g_done),
        .wr_en(gwr_en), .wr_addr(gwr_addr), .wr_re(gwr_re), .wr_im(gwr_im),
        .rd_addr(grd_addr), .rd_re(grd_re), .rd_im(grd_im),
        .st_addr(9'd0), .st_n(), .st_g()
    );

    // ---------------------------------------------------------------- 窗乘
    // 样点是 Q1.22，窗是 Q1.15；乘完右移 15 保持 Q1.22。
    // 注意：Verilog 里 a*b 的结果宽度 = 两个操作数里较宽的那个，32 bit 的 Q1.22
    // 乘 16 bit 窗会溢出（Q1.22 满幅 2^23 × 2^15 = 2^38），所以两个操作数都要先
    // 扩到 48 bit 再乘 —— 这一条在 dn_gain 里已经踩过一次了。
    reg signed [47:0] mul_a;      // 分析窗乘（临时量，组合用）
    reg signed [47:0] mul_s;      // 合成窗乘（临时量，组合用）
    reg        [8:0]  haddr;      // hist 读地址（寄存，同步读存储器）
    reg signed [15:0] hdat_r;     // hist 同步读回的数据（BRAM 输出寄存）
    reg        [8:0]  oaddr;      // ola 读地址（寄存，同步读存储器）
    reg signed [31:0] ola_r;      // ola 同步读回的数据
    wire signed [15:0] hdat = hdat_r;

    assign busy = (st != S_IDLE);

    // ---------------------------------------------------------------- 主状态机
    integer hb;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            st <= S_IDLE; k <= 10'd0; k9 <= 9'd0; ph <= 2'd0;
            wptr <= 9'd0; acc <= 10'd0; fb <= 10'd0;
            fft_start <= 1'b0; fft_inv <= 1'b0;
            fwr_en <= 1'b0; fwr_addr <= 9'd0; fwr_re <= 32'sd0; fwr_im <= 32'sd0;
            frd_addr <= 9'd0;
            g_start <= 1'b0; g_warmup <= 1'b0;
            gwr_en <= 1'b0; gwr_addr <= 9'd0; gwr_re <= 32'sd0; gwr_im <= 32'sd0;
            grd_addr <= 9'd0;
            out_valid <= 1'b0; out_l <= 16'sd0;
            frame_count <= 32'd0;
            haddr <= 9'd0;
        end else begin
            fft_start <= 1'b0;
            g_start   <= 1'b0;
            fwr_en    <= 1'b0;
            gwr_en    <= 1'b0;
            out_valid <= 1'b0;

            // 样点先进 hist（直通模式下也一样，保证延迟一致）。
            // 存储器本身的写入放在文件末尾那个「无复位」的 always 块里 ——
            // 放在带异步复位的块里 Vivado 会报 "RAM is sensitive to
            // asynchronous reset signal"，整个阵列退化成触发器。
            if (in_valid) begin
                wptr <= wptr + 9'd1;
                if (acc != 10'd512) acc <= acc + 10'd1;
            end

            case (st)
                S_IDLE: begin
                    if (acc >= 10'd512) begin
                        // 必须攒满整个分析窗（512 点）才开帧，否则窗口后半截还没写进来
                        // 开帧这一拍如果正好也有样点进来，它必须算进下一帧，
                        // 否则每帧少计 1 个样点、fb 与数据逐步错位（帧边界会出跳变）
                        acc <= in_valid ? 10'd257 : 10'd256;
                        fb  <= wptr;             // 锁存本帧起点（hist 只有 512 深，wptr 本身即窗口起点）
                        k   <= 10'd0;
                        ph  <= 2'd0;
                        st  <= S_SMP;
                    end
                end

                // 三拍：给地址 -> 同步读回 -> 乘窗写 FFT。
                // 用同步读是为了让 hist 落进 BRAM：组合读会被综合成触发器 + 巨型
                // 多路选择器（实测 168k LUT / 317% 器件，一个 BRAM 都不用）。
                // 每帧 512 点 × 3 拍 = 1536 拍，而一帧有 266k 拍预算，完全够。
                S_SMP: begin
                    if (ph == 2'd0) begin
                        haddr <= (fb + k) & 10'h1FF;   // 这一帧的起点
                        ph    <= 2'd1;
                    end else if (ph == 2'd1) begin
                        ph     <= 2'd2;        // 读回由末尾的存储器块负责
                    end else begin
                        // 分析窗：Q1.22 * Q1.15 >> 15（先扩 48 bit 再乘）
                        mul_a    = $signed({{16{hdat[15]}}, hdat}) <<< 7;   // 符号扩展到 48 bit
                        fwr_en   <= 1'b1;
                        fwr_addr <= k[8:0];
                        fwr_re   <= (mul_a * win_v) >>> 15;
                        fwr_im   <= 32'sd0;
                        ph       <= 2'd0;
                        if (k == 10'd511) st <= S_FFT;
                        else              k  <= k + 10'd1;
                    end
                end

                S_FFT: begin
                    if (ph == 2'd0) begin
                        fft_inv   <= 1'b0;
                        fft_start <= 1'b1;
                        ph        <= 2'd1;
                    end else if (fft_done) begin
                        ph <= 2'd0; k <= 10'd0;
                        st <= (SKIP_GAIN != 0) ? S_IFFT : S_CPIN;
                    end
                end

                // 三拍：0=给地址，1=等一拍（读端口是寄存器输出），2=取数并写下一级
                S_CPIN: begin
                    if (ph == 2'd0) begin
                        frd_addr <= k[8:0];
                        ph       <= 2'd1;
                    end else if (ph == 2'd1) begin
                        ph <= 2'd2;
                    end else begin
                        gwr_en   <= 1'b1;
                        gwr_addr <= k[8:0];
                        gwr_re   <= frd_re;
                        gwr_im   <= frd_im;
                        ph       <= 2'd0;
                        if (k == 10'd511) st <= S_GAIN;
                        else              k  <= k + 10'd1;
                    end
                end

                S_GAIN: begin
                    if (ph == 2'd0) begin
                        g_warmup <= (frame_count < WARMUP);
                        g_start  <= 1'b1;
                        ph       <= 2'd1;
                    end else if (g_done) begin
                        ph <= 2'd0; k <= 10'd0;
                        st <= S_CPBK;
                    end
                end

                S_CPBK: begin
                    if (ph == 2'd0) begin
                        grd_addr <= k[8:0];
                        ph       <= 2'd1;
                    end else if (ph == 2'd1) begin
                        ph <= 2'd2;
                    end else begin
                        fwr_en   <= 1'b1;
                        fwr_addr <= k[8:0];
                        fwr_re   <= grd_re;
                        fwr_im   <= grd_im;
                        ph       <= 2'd0;
                        if (k == 10'd511) st <= S_IFFT;
                        else              k  <= k + 10'd1;
                    end
                end

                S_IFFT: begin
                    if (ph == 2'd0) begin
                        fft_inv   <= 1'b1;
                        fft_start <= 1'b1;
                        ph        <= 2'd1;
                    end else if (fft_done) begin
                        ph <= 2'd0; k <= 10'd0;
                        st <= S_SYN;
                    end
                end

                // 乘合成窗并累加到 OLA。ola 是读-改-写存储器，必须拆成
                // "同步读 -> 下一拍写回"，否则综合成 512×32 个触发器 + 加法树。
                S_SYN: begin
                    if (ph == 2'd0) begin
                        frd_addr <= k[8:0];
                        oaddr    <= (fb + k) & 10'h1FF;
                        ph       <= 2'd1;     // ola 读由末尾的存储器块负责
                    end else if (ph == 2'd1) begin
                        ph <= 2'd2;
                    end else begin
                        // ola 的写也由末尾的存储器块负责（单写口，数据在里面选）
                        ph <= 2'd0;
                        if (k == 10'd511) begin
                            k  <= 10'd0;
                            st <= S_OUT;
                        end else begin
                            k <= k + 10'd1;
                        end
                    end
                end

                // 吐这一帧的前 256 点（后半帧保留给下一帧叠加）。
                // 同样拆成"读 -> 清零 + 输出"两拍，让 ola 保持 1 读 1 写。
                S_OUT: begin
                    if (ph == 2'd0) begin
                        oaddr <= (fb + k) & 10'h1FF;
                        ph    <= 2'd1;        // ola 读由末尾的存储器块负责
                    end else begin
                        out_valid  <= 1'b1;
                        out_l      <= (ola_r >>> 7) > 32767 ?
                                      16'sd32767 :
                                      ((ola_r >>> 7) < -32768 ?
                                       16'sd32768 : (ola_r >>> 7));
                        ph <= 2'd0;
                        if (k == 10'd255) begin
                            k <= 10'd0;
                            frame_count <= frame_count + 32'd1;
                            st <= S_IDLE;
                        end else begin
                            k <= k + 10'd1;
                        end
                    end
                end

                default: st <= S_IDLE;
            endcase
        end
    end

    //=========================================================================
    // 存储器（独立的 always 块，绝不放复位、绝不放别的逻辑）
    //   hist : 1 写（in_valid）+ 1 读（S_SMP 给地址，下一拍取数）
    //   ola  : 1 写（清零 / 叠加 / 吐完清零，三选一）+ 1 读（S_SYN、S_OUT）
    // 综合规则（UG901）：带异步复位、或一个进程里出现多个写语句，都会让
    // Vivado 放弃 BRAM 推断。上一版就是踩了这两条，168k LUT / 317% 器件。
    //=========================================================================
    wire              hist_we = in_valid;
    wire [8:0]        hist_wa = wptr;
    wire signed [15:0] hist_wd = in_l;

    wire signed [47:0] ola_prod = (frd_re * win_v) >>> 15;   // 48 bit 符号扩展后乘
    wire              ola_we   = mem_init |
                                 ((st == S_SYN) && (ph == 2'd2)) |
                                 ((st == S_OUT) && (ph == 2'd1));
    wire [8:0]        ola_wa   = mem_init ? icnt : oaddr[8:0];
    wire signed [31:0] ola_wd  = mem_init ? 32'sd0 :
                                 ((st == S_SYN) ? (ola_r + ola_prod) : 32'sd0);
    wire              ola_re   = ((st == S_SYN) || (st == S_OUT)) && (ph == 2'd0);

    always @(posedge clk) begin
        if (hist_we) hist[hist_wa] <= hist_wd;
        hdat_r <= hist[haddr];

        if (ola_we) ola[ola_wa] <= ola_wd;
        if (ola_re) ola_r <= ola[(fb + k) & 10'h1FF];
    end

endmodule
