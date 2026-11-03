//=============================================================================
// dn_div24.v --- 24 拍恢复除法器（无符号）
//
// 用途：谱减里要算 alpha*noise/P（≤1 的比值），一次一 bin，24 位商够用
// （Q1.24 的比值误差 < 6e-8，对 -90 dB 的定点底噪毫无影响）。
//
// 约定：n 48 位、d 24 位、调用方保证 n < d*2^24（即商不溢出），
// 本模块不检查溢出，商取低 24 位。
//=============================================================================
`timescale 1ns / 1ps

module dn_div24 (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               start,
    input  wire [47:0]        n,
    input  wire [23:0]        d,
    output reg  [23:0]        q,
    output reg  [24:0]        rem,
    output reg                busy,
    output reg                done
);

    reg [24:0] r;      // 余数（多 1 位容纳左移）
    reg [47:0] nn;     // 被除数寄存器
    reg [23:0] dd;
    reg [4:0]  i;
    reg [23:0] qq;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            busy <= 1'b0;
            done <= 1'b0;
            q    <= 24'd0;
            rem  <= 25'd0;
            r    <= 25'd0;
            qq   <= 24'd0;
            i    <= 5'd0;
        end else begin
            done <= 1'b0;
            if (start && !busy) begin
                busy <= 1'b1;
                nn   <= n;
                dd   <= d;
                r    <= {1'b0, n[47:24]};
                qq   <= 24'd0;
                i    <= 5'd23;
            end else if (busy) begin
                // 余数左移一位，带下被除数的一位
                if ({r[23:0], nn[i]} >= {1'b0, dd}) begin
                    r  <= {r[23:0], nn[i]} - {1'b0, dd};
                    qq <= qq | (24'd1 << i);
                end else begin
                    r  <= {r[23:0], nn[i]};
                end

                if (i == 5'd0) begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    q    <= qq | (({r[23:0], nn[i]} >= {1'b0, dd}) ? 24'd1 : 24'd0);
                    rem  <= ({r[23:0], nn[i]} >= {1'b0, dd})
                            ? ({r[23:0], nn[i]} - {1'b0, dd}) : {r[23:0], nn[i]};
                end else begin
                    i <= i - 5'd1;
                end
            end
        end
    end

endmodule
