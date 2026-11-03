//=============================================================================
// dn_isqrt.v --- 16 拍整数平方根（逐位法）
//
// 输入 x（32 位无符号），输出 s = floor(sqrt(x))（16 位）。
// 谱减里需要 g = sqrt(1 - alpha*noise/P)，令 t = (1<<24) - q（Q1.24），
// 则 g_q15 = floor(sqrt(t << 6))，直接喂本模块即可（见 dn_gain 的用法）。
//=============================================================================
`timescale 1ns / 1ps

module dn_isqrt (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    input  wire [31:0] x,
    output reg  [15:0] s,
    output reg         busy,
    output reg         done
);

    reg [33:0] xx;
    reg [18:0] rem;
    reg [15:0] root;
    reg [4:0]  i;
    reg [18:0] test;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            busy <= 1'b0;
            done <= 1'b0;
            s    <= 16'd0;
            xx   <= 34'd0;
            rem  <= 19'd0;
            root <= 16'd0;
            i    <= 5'd15;
        end else begin
            done <= 1'b0;
            if (start && !busy) begin
                busy <= 1'b1;
                xx   <= {2'b00, x};
                rem  <= 19'd0;
                root <= 16'd0;
                i    <= 5'd15;
            end else if (busy) begin
                test = {root, 2'b01};
                if ({rem[16:0], xx[2*i +: 2]} >= test) begin
                    rem  <= {rem[16:0], xx[2*i +: 2]} - test;
                    root <= {root[14:0], 1'b1};
                end else begin
                    rem  <= {rem[16:0], xx[2*i +: 2]};
                    root <= {root[14:0], 1'b0};
                end

                if (i == 5'd0) begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    s    <= ({rem[16:0], xx[1:0]} >= {root, 2'b01})
                            ? {root[14:0], 1'b1} : {root[14:0], 1'b0};
                end else begin
                    i <= i - 5'd1;
                end
            end
        end
    end

endmodule
