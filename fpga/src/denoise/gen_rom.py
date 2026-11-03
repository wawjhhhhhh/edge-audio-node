"""把 vec/*.hex 转成可综合的 ROM 模块（供上板用；仿真也走同一份，保证逐位一致）。

生成 dn_rom.v：
  module dn_win_rom    输入 addr[8:0]  输出 q[15:0]   （512 点 sqrt-Hann，Q1.15）
  module dn_tw_rom     输入 addr[8:0]  输出 cos[23:0] / sin[23:0]（旋转因子，Q1.22）

为什么不用 $readmemh：RTL 会被打包成 IP 放进 BD，综合时的工作目录不是仓库，
相对路径的 .hex 找不到；initial 块里的常量数组则跟着 RTL 走，任何地方都不会丢。
"""

import io

VEC = r'D:\deepseek herness\work\eth_audio\pl\denoise\vec'
OUT = r'D:\deepseek herness\work\eth_audio\pl\denoise\rtl\dn_rom.v'


def load(name):
    return [int(x.strip(), 16) for x in io.open(VEC + '\\' + name) if x.strip()]


win = load('win.hex')
cos = load('tw_cos.hex')
sin = load('tw_sin.hex')
assert len(win) == 512 and len(cos) == 512 and len(sin) == 512

L = []
L.append('//=============================================================================')
L.append('// dn_rom.v --- 由 gen_rom.py 从 vec/*.hex 自动生成，请勿手改')
L.append('//    dn_win_rom : 512 点 sqrt-Hann 合成/分析窗（Q1.15）')
L.append('//    dn_tw_rom  : 512 点 FFT 旋转因子（Q1.22）')
L.append('//=============================================================================')
L.append('`timescale 1ns / 1ps')
L.append('')
L.append('module dn_win_rom (')
L.append('    input      [8:0]  addr,')
L.append('    output reg [15:0] q')
L.append(');')
L.append('    always @(*) begin')
L.append('        case (addr)')
for i, v in enumerate(win):
    L.append("            9'd%-3d: q = 16'h%04x;" % (i, v))
L.append('            default: q = 16\'h0000;')
L.append('        endcase')
L.append('    end')
L.append('endmodule')
L.append('')
L.append('module dn_tw_rom (')
L.append('    input      [8:0]  addr,')
L.append('    output reg [23:0] c,')
L.append('    output reg [23:0] s')
L.append(');')
L.append('    always @(*) begin')
L.append('        case (addr)')
for i, (cv, sv) in enumerate(zip(cos, sin)):
    L.append("            9'd%-3d: begin c = 24'h%06x; s = 24'h%06x; end" % (i, cv, sv))
L.append('            default: begin c = 24\'h000000; s = 24\'h000000; end')
L.append('        endcase')
L.append('    end')
L.append('endmodule')

io.open(OUT, 'w', encoding='utf-8', newline='\n').write('\n'.join(L) + '\n')
print('wrote', OUT, len(L), 'lines')
