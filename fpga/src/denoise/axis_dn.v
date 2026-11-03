//=============================================================================
// axis_dn.v --- 把 dn_top（16 bit 单声道帧处理器）包装成 AXI4-Stream 通道
//
// 为什么需要这一层：
//   dn_top 是"攒满 512 点算一帧、一帧吃 256 点吐 256 点"的突发式处理器（44k 拍算完，
//   然后一口气吐 256 点）；AXI-Stream 是逐拍流动、随时可能被下游反压的接口。
//   所以入口和出口各需要一个 FIFO 吸收突发，tlast 由出口的 beat 计数器重新生成。
//
// 数据格式：AXIS 32 bit = {R[15:0], L[15:0]}。只处理左声道（板载麦克风单声道，
//   左右内容相同），高 16 位原样透传 —— 右声道始终是原始信号，可直接做 AB 对比。
//
// 反压：
//   - 入口 FIFO 满 -> s_axis_tready 拉低（唯一背压点）
//   - 出口 FIFO 满且有新样点要写 -> 丢弃 + drop_cnt++（不阻塞入口，避免死锁）
//
// bypass=1：数据从入口 FIFO 直接搬到出口 FIFO，tlast 照常重新生成。
//   用于"先只加模块、验直通不破坏 100 fps 链路"这一步。
//=============================================================================
`timescale 1ns / 1ps

module axis_dn #(
    parameter BEATS_PER_PKT = 48,          // 保持兼容（未再使用）
    parameter FRAME_BEATS   = 480,         // 10 ms @ 48 kHz = 480 个立体声字
    parameter LOG2_IN       = 9,          // 512 beat
    parameter LOG2_OUT      = 9
)(
    input  wire        clk,
    input  wire        rst_n,

    input  wire        bypass,
    input  wire [1:0]  nz,             // 底噪注入档位 0=关 1/2/3=小/中/大（PL 侧、谱减之前）
    output wire        busy,
    output wire [31:0] frame_count,
    output wire [15:0] drop_cnt,

    input  wire [31:0] s_axis_tdata,
    input  wire        s_axis_tvalid,
    input  wire        s_axis_tlast,      // 入口不看 tlast：包边界由出口重新生成
    output wire        s_axis_tready,

    output wire [31:0] m_axis_tdata,
    output wire        m_axis_tvalid,
    output wire        m_axis_tlast,
    input  wire        m_axis_tready
);

    //-------------------------------------------------------------------------
    // 入口 FIFO（异步读）
    //-------------------------------------------------------------------------
    // 存储体做成独立模块（单一写语句 + 组合读），Vivado 才能推断成
    // 分布式 RAM；写在 always 里带多个分支时它会报 "Invalid write to RAM"，
    // 整个 FIFO 退化成触发器。
    // {tlast, data} 一起进 FIFO：tlast 必须跟着它那一个 beat 走，
    // 否则下游（AXI DMA S2MM）会在错误的 beat 上结束传输 ——
    // 实测症状是「每 48 个 beat 结束一次」，DMA 于是每 1 ms 就报一次
    // 1920 字节传输完成，PS 侧帧率整整快 10 倍（100 fps 变 ~1000 fps）。
    //-------------------------------------------------------------------------
    // 底噪注入（PL 侧，谱减之前）
    //-------------------------------------------------------------------------
    // LFSR 白噪声：x^32+x^22+x^2+x+1 抽头，每个时钟走一步。取中间 16 位当样点，
    // 按档位右移缩放 —— 右移比乘法省，且"小/中/大"只要相对关系对就够。
    reg  [31:0] nz_lfsr;
    always @(posedge clk) begin
        if (!rst_n) nz_lfsr <= 32'h1234_5678;
        else        nz_lfsr <= {nz_lfsr[30:0],
                               nz_lfsr[31] ^ nz_lfsr[21] ^ nz_lfsr[1] ^ nz_lfsr[0]};
    end

    wire signed [15:0] nz_raw = $signed({nz_lfsr[31], nz_lfsr[23:9]});
    wire signed [15:0] nz_val = (nz == 2'd0) ? 16'sd0 :
                                (nz == 2'd1) ? (nz_raw >>> 6) :   // ±512
                                (nz == 2'd2) ? (nz_raw >>> 4) :   // ±2048
                                               (nz_raw >>> 3);    // ±4096

    wire signed [15:0] nz_l_in = $signed(s_axis_tdata[15:0]);
    wire signed [15:0] nz_r_in = $signed(s_axis_tdata[31:16]);
    wire signed [16:0] nz_l_sum = nz_l_in + nz_val;
    wire signed [16:0] nz_r_sum = nz_r_in + nz_val;

    wire signed [15:0] nz_l_out = (nz_l_sum > 17'sd32767)  ? 16'sd32767  :
                                  (nz_l_sum < -17'sd32768) ? -16'sd32768 : nz_l_sum[15:0];
    wire signed [15:0] nz_r_out = (nz_r_sum > 17'sd32767)  ? 16'sd32767  :
                                  (nz_r_sum < -17'sd32768) ? -16'sd32768 : nz_r_sum[15:0];

    wire [32:0] in_q;
    wire        in_we    = s_axis_tvalid && s_axis_tready;
    wire [32:0] in_wdata = {s_axis_tlast, nz_r_out, nz_l_out};
    dn_afifo512 u_in_fifo (
        .clk(clk), .we(in_we),
        .waddr(in_wp[LOG2_IN-1:0]), .wdata(in_wdata),
        .raddr(in_rp[LOG2_IN-1:0]), .rdata(in_q)
    );
    reg [LOG2_IN:0] in_wp, in_rp;
    wire in_empty = (in_wp == in_rp);
    wire in_full  = (in_wp[LOG2_IN] != in_rp[LOG2_IN]) &&
                    (in_wp[LOG2_IN-1:0] == in_rp[LOG2_IN-1:0]);

    assign s_axis_tready = ~in_full;

    //-------------------------------------------------------------------------
    // 出口 FIFO（异步读，AXIS 直接由 FIFO 头驱动）
    //-------------------------------------------------------------------------
    // dn_top 的输出先声明（实例化在后面，Verilog-2001 要先声明后使用）
    wire        dn_out_valid;
    wire [15:0] dn_out_l;

    // 处理通路的帧边界（直通通路不用它：tlast 跟着原始 beat 走）
    reg  [9:0]  dn_beat;
    wire        dn_tlast = (dn_beat == FRAME_BEATS-1);

    wire [32:0] out_q;
    reg [LOG2_OUT:0] out_wp, out_rp;
    wire out_empty = (out_wp == out_rp);
    wire out_full  = (out_wp[LOG2_OUT] != out_rp[LOG2_OUT]) &&
                     (out_wp[LOG2_OUT-1:0] == out_rp[LOG2_OUT-1:0]);

    // Verilog-2001 要求先声明后使用：in_pop 依赖 out_full / held_v，
    // 所以它的声明必须排在这两个信号之后（之前放在 129 行会报
    // VRFC 10-3380 'in_pop' is used before its declaration）。
    reg  [31:0] held_word;      // 已从入口 FIFO 取走、还没喂给 dn_top 的 beat
    reg         held_v;

    wire        in_pop    = ~in_empty && (bypass ? ~out_full : ~held_v);
    wire        out_we_bp = bypass && in_pop;
    wire        out_we_dn = !bypass && dn_out_valid && !out_full;
    wire        out_we    = out_we_bp || out_we_dn;
    // 直通：{tlast, data} 原样搬；处理：单声道复制到左右两个槽，
    // tlast 由 dn_tlast（每 FRAME_BEATS 个 beat 一次）生成。
    wire [32:0] out_wdata = out_we_bp ? in_q
                                      : {dn_tlast, dn_out_l, dn_out_l};
    dn_afifo512 u_out_fifo (
        .clk(clk), .we(out_we),
        .waddr(out_wp[LOG2_OUT-1:0]), .wdata(out_wdata),
        .raddr(out_rp[LOG2_OUT-1:0]), .rdata(out_q)
    );


    assign m_axis_tdata  = out_q[31:0];
    assign m_axis_tvalid = ~out_empty;
    assign m_axis_tlast  = out_q[32];

    //-------------------------------------------------------------------------
    // dn_top
    //-------------------------------------------------------------------------
    // dn_out_valid / dn_out_l 已经在文件上部提前声明（这里不再重复）
    wire        dn_busy;
    wire [31:0] dn_frames;

    reg         dn_in_valid;
    reg  [15:0] dn_in_l;

    dn_top #(
        .WARMUP (60)
    ) u_dn (
        .clk        (clk),
        .rst_n      (rst_n),
        .bypass     (bypass),
        .in_valid   (dn_in_valid),
        .in_l       (dn_in_l),
        .out_valid  (dn_out_valid),
        .out_l      (dn_out_l),
        .frame_count(dn_frames),
        .busy       (dn_busy)
    );

    assign busy        = dn_busy;
    assign frame_count = dn_frames;

    //-------------------------------------------------------------------------
    // 入口取数与分发
    //-------------------------------------------------------------------------
    reg  [15:0] drop_r;

    // 直通时每拍搬一个；处理时每拍只取一个并暂存（dn_top 按自己的节拍消化）

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            in_wp       <= 0;
            in_rp       <= 0;
            out_wp      <= 0;
            out_rp      <= 0;
            held_v      <= 1'b0;
            held_word   <= 32'd0;
            dn_in_valid <= 1'b0;
            dn_in_l     <= 16'd0;
            drop_r      <= 16'd0;
            dn_beat     <= 10'd0;
        end else begin
            // ---- input FIFO 写指针 ----
            if (in_we) in_wp <= in_wp + 1'b1;

            // ---- input FIFO 读 ----
            dn_in_valid <= 1'b0;
            if (in_pop) begin
                in_rp <= in_rp + 1'b1;
                if (bypass) begin
                    out_wp <= out_wp + 1'b1;
                end else begin
                    held_word <= in_q;
                    held_v    <= 1'b1;
                end
            end

            // ---- feed dn_top: it samples on its own frame cadence ----
            if (!bypass && held_v) begin
                dn_in_valid <= 1'b1;
                dn_in_l     <= held_word[15:0];
                held_v      <= 1'b0;
            end

            // ---- dn_top output -> output FIFO (mono copied to both halves) ----
            if (!bypass && dn_out_valid) begin
                if (!out_full) begin
                    out_wp <= out_wp + 1'b1;
                end else begin
                    drop_r <= drop_r + 16'd1;
                end
            end

            // ---- output handshake ----
            if (m_axis_tvalid && m_axis_tready) begin
                out_rp <= out_rp + 1'b1;
            end

            // ---- 处理通路的帧边界计数 ----
            if (out_we_dn) begin
                dn_beat <= dn_tlast ? 10'd0 : dn_beat + 10'd1;
            end
        end
    end


    assign drop_cnt = drop_r;

endmodule

//-----------------------------------------------------------------------------
// 512 深、32 bit 的异步读 FIFO 存储体（组合读 == 分布式 RAM）
//-----------------------------------------------------------------------------
module dn_afifo512 #(
    parameter W = 33
) (
    input  wire          clk,
    input  wire          we,
    input  wire [8:0]    waddr,
    input  wire [W-1:0]  wdata,
    input  wire [8:0]    raddr,
    output wire [W-1:0]  rdata
);
    (* ram_style = "distributed" *) reg [W-1:0] mem [0:511];
    always @(posedge clk) begin
        if (we) mem[waddr] <= wdata;
    end
    assign rdata = mem[raddr];
endmodule
