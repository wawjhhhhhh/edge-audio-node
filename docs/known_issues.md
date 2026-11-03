# 已知问题与失败分析

这一节记录**没能藏住的问题**。竞赛评分里"失败分析"是单列的一项，所以这里不写"功能都正常"，只写哪条路走死了、证据是什么、怎么绕、下一步怎么修。

---

## 问题 1：任何一次"重新综合 + 实现"得到的比特流，麦克风采集 0 包（最高优先级）

### 1.1 现象

| 比特流 | 来源 | 采集结果 |
|---|---|---|
| `eth_audio_top_20261029.bit`（4 045 768 B，SHA256 前缀 `931DA562B98C`） | 2026-10-29 00:20 归档 | **100 fps，正常** |
| 同一份 RTL 重新综合 + 实现的任何 bit | 10-30 之后多次构建 | **0 包**，`fps=0` |

固件是同一份 `app_ea.elf`（633 976 B，`10DD461D1834`）。PC 侧、UDP、DMA 寄存器、codec I2C 配置全部排除过——**坏的一定在 PL 的音频采集域**。

### 1.2 现场读数（板子上抓的，不是推断）

| 观测量 | 读数 | 含义 |
|---|---|---|
| `cnt_rx_push`（`0x41200008` 相关计数） | 两次读取均为 `0x008B1022` | 采集**一次都没推进** |
| `rx_ovf` | `139`，不增长 | 溢出计数冻结 ⇒ 没有新数据写入 |
| `m_axis_tvalid` 占空比 | ≈ 1/1000（示波器） | 几乎恒低，等于 FIFO 一直空 |
| `rempty` | 恒 1 | 写指针没动过 |

⇒ `wgray` 不前进 ⇒ 读侧 `rempty` 恒 1 ⇒ `m_axis_tvalid` 恒 0 ⇒ DMA 收不到帧 ⇒ PS 没数据可发 ⇒ **0 包**。

### 1.3 根因

**（a）`rx_done` 只由 LRC 边沿决定，与 ADC 数据无关。**

`audio_receive.v`：

```verilog
assign lrc_edge = aud_lrc ^ aud_lrc_d0;          // 45
always @(posedge aud_bclk ...) begin
    if (lrc_edge) rx_cnt <= 0;                    // 56-60
    else if (rx_cnt <= WL) rx_cnt <= rx_cnt + 1;  // 61-62（6 位计数器，WL=16，计到 17 就停）
    ...
    else if (rx_cnt == WL) begin rx_done <= 1; adc_data <= adc_data_t; end  // 76-87
end
```

只要 `lrc_edge` 一帧都没被识别，`rx_cnt` 会停在 17，`rx_done` 在复位后最多只出现**一次**。那一次若正好落在左声道槽（`aud_lrc == 0`），`audio_axis_core` 连 push 都不会做——**结构上就等于 0 包**，且与 ADC 是否出声完全无关。这与 1.2 的读数逐条吻合。

**（b）`aud_bclk` / `aud_lrc` 没有时钟约束，这一域根本不参与时序分析。**

`impl_1/eth_audio_top_timing_summary_routed.rpt` 的 `check_timing` 原文：

```
no_clock (17013)
  ... 16923 pins under root clock pin u_bd/eth_audio_i/audio_axis_core_0/inst/u_slave_signal_gen/aud_bclk_reg/Q
unconstrained_internal_endpoints (34565)
TIMING-17  Non-clocked sequential cell  (≥1000，报告截顶)
```

`aud_bclk` 已经上了 BUFG（`CLKAG_.../CLK_BUFG`），但 XDC 里只有 `aud_mclk_OBUF` 的时钟组，**没有 `create_clock aud_bclk / aud_lrc`**。于是这 16923 个触发器的建立/保持**全靠布线运气**——同一个网表重新实现一次，结果可以完全不同。黄金版是运气好的那一次。

**（c）AFIFO 的存储阵列被综合成触发器，把这一域摊满了整个 die。**

`fpga/src/rtl/afifo.v:62` 的写出现在带异步复位的 always 块里：

```verilog
always @(posedge wclk or negedge wrst_n) begin
    if (!wrst_n) begin wbin <= 0; wgray <= 0; end
    else begin ... mem[wbin[AW-1:0]] <= wdata; end   // 62
end
```

综合日志原文（`fpga/report/audio_core_ooc_synth.log`）：

```
WARNING: [Synth 8-7137] Register mem_reg in module afifo has both Set and reset with same priority.
WARNING: [Synth 8-4767] Trying to implement RAM 'mem_reg' in registers. Block RAM or DRAM implementation is not possible.
  1: RAM is sensitive to asynchronous reset signal. this RTL style is not supported.
WARNING: [Synth 8-13159] Potential Runtime issue for RAM 'mem_reg' since this is dissolved into (16384) registers bits.
```

`AW=9`（BD 覆盖）⇒ 每个 FIFO 的 `mem` 是 512 × 32 = **16384 个触发器**，两个 FIFO 共 32768。OOC 综合利用率：**Slice Registers 33700（31.67 %）、Block RAM 0、LUT 10398**。这也是 `no_clock` 里那 16923 个引脚的来源。

### 1.4 为什么"重建就坏、老 bit 就好"（RTL 是同一份）

`git diff 014ef2e HEAD -- work/audio_axis/rtl work/eth_audio/rtl` 显示：`audio_axis_core.v`、`audio_receive.v`、`slave_signal_gen.v`、`afifo.v` **逐字节相同**。BD 参数（`AW=9 / BEATS_PER_PKT=480 / LOOPBACK=0`）在黄金提交里就已经是这个值。

所以差别不在 RTL，而在**约束与实现**：

1. 一个 16923 触发器、无时钟约束的域，实现结果本来就是随机的；
2. AFIFO 用 3 万个触发器实现 2 KB 存储，把这一域摊到整个芯片，布线延迟随机性被放大；
3. XDC 有过一次差异：黄金版 `set_clock_groups` 里有 `aud2_mclk_OBUF`，中间某版构建脚本重写 XDC 时丢过，后来补回——补回是对的，但它说明"约束是被脚本重写的，很容易悄悄丢一条"。

**结论：这不是一个"某个信号接错了"的 bug，而是一个"没有约束的域 + 畸形实现"的稳定性 bug。**它必然表现为"有的构建能跑、有的不能"，而不是每次都坏。

### 1.5 修复方案（四条，按优先级）

| # | 改什么 | 怎么改 | 验收 |
|---|---|---|---|
| 1 | 补时钟约束 | `create_clock -name aud_bclk_pl -period 650.8 [get_pins .../aud_bclk_reg/Q]`；`create_clock -name aud_lrc_pl -period 20833.333 [...]`；把 `aud_bclk/aud_lrc` 归入 audio 时钟组；LRC → BCLK 域按数据路径给 `set_max_delay -datapath_only` | `check_timing` 里 `no_clock` 从 17013 降到 ~90（只剩 I2C 的复位寄存器），TIMING-17 归零 |
| 2 | 让 AFIFO 落进 BRAM | 把 `mem` 的写搬出复位块（复位只作用于指针），加 `(* ram_style = "block" *)` | OOC 综合报告 Block RAM ≥ 2、Slice Registers 从 33700 掉到 ~1000 |
| 3 | LRC 改成同步采样 | `aud_lrc` 先过 2 级 `(* ASYNC_REG *)` 触发器再取边沿，不要直接与 `aud_lrc_d0` 相异或 | 同一份 RTL 连续重建 3 次，3 次都 100 fps |
| 4 | 建立回归 | 每次构建后跑 `metrics/scripts/chk_link_now.py`，`fps` 必须 ≥ 95，`lost = 0` | 进 CI 脚本 |

> ⚠️ 写约束时的数值陷阱：文档旧稿写 `-period 325.521`（3.072 MHz），**是错的**。`slave_signal_gen.v:65` 是 `bclk_div_cnt == 8'd3`，4 个 MCLK 翻转一次 ⇒ 周期 8 × 81.35 ns = **650.8 ns = 1.536 MHz**。用错周期会让约束比实际松一倍，等于没约束。

### 1.6 这一节的教训

- **"能跑"和"能复现"是两件事**。这个工程曾经"能跑"，但它依赖一次运气好的布局；一旦重新实现就回到 0。
- **不受约束的域不是"不检查"，是"随机"**。Vivado 不会报错，只会给你一个可能能用的比特流。
- **一个 512 深的 FIFO 写成 16384 个触发器，编译器用 WARNING 提醒了三次**（8-7137 / 8-4767 / 8-13159），当时没人看日志。日志里的 WARNING 就是最早的 bug 报告。

---

## 问题 2：PL 谱减核写完了，但没接进数据通路

`fpga/src/denoise/` 有完整的 `axis_dn / dn_top / fft512 / dn_gain / dn_rom`，配 8 个 testbench 和 Python 定点黄金模型，仿真能过。但：

```
grep -E "axis_dn|dn_top|fft512|dn_gain" \
  work/eth_audio/bd/build_eth_audio.tcl \
  work/eth_audio/rtl/audio_axis_core.v \
  work/eth_audio/rtl/eth_audio_top.v
→ 零命中
```

**当前 bit 的 PL 只做**：I2S 收发、AFIFO 跨时钟、AXIS/DMA、codec I2C 配置、时钟。控制台上的 `'3'`（PL 谱减）档走的是**直通**，`dac_mode` 类字段会诚实回 `3`（未生效）。

接入需要三步，验收口径：

1. 在 BD 里把 `axis_dn` 插到 `audio_axis_core/m_axis` 与 `acc_rx/S_AXIS` 之间（两侧同为 50 MHz，不需要额外 CDC）；
2. 把 AXI GPIO `0x41200000` bit0 接到 `axis_dn/bypass`，让控制台的 `'3'` 真正生效；
3. 用 `metrics/scripts/ab_pl.py` 做同源 A/B（同一段噪声同时进旁路与谱减），量化 SNR 提升与 THD 退化。

**注意面积**：当前实现 Slice 已用到 **99.40 %**（13 220/13 300），直接插进去几乎肯定布不通，需要先做优化（`fft512` 的蝶形复用、增益表 ROM 共享）。

---

## 问题 3：RNNoise 在这颗 ARM 上跑不到实时

`app_ea_main.c:410-520` 的 RNNoise 批量环实测 **20.07 ms 处理一帧 10 ms 音频**，能力约 **49 fps**。当前用"一半帧直通"的策略保证连续性，代价是降噪只在半数帧上生效。

这也是问题 2 的直接动因：把谱减搬到 PL，是为了同时拿回 100 fps 和确定的处理延迟。

---

## 问题 4：调试寄存器 `res[]` 的布局已经不是文档里写的那套

旧交接文档 §5.5 给了 `res[0..15]` 的语义表。实测：`res[0] = 0x4C4E4B31`（`'LNK1'`），`res[1..14]` 全 0，`res[15] = 0x0032DA29`，**两次读取完全相同**（不再更新）。⇒ 新固件不再往 `res[]` 写活数据，那张表已过期。

现在可用的活读数是 **UDP 50508 的 15 字节状态包**（见 `docs/interface.md` 第 3 节）与控制台 `/api/state`。

---

## 问题 5：`0x41200008` 在归档 bit 上读回的不是 RTL 里的 `gpio_word`

RTL 里该寄存器是 `{cnt_rx_ovf[7:0], cnt_tx_unf[6:0], key_state, 16'h1022}`（低 16 位应为 `0x1022`）。但在 2026-10-29 的 bit 上实测读回 `0xA5B16D1E`。

⇒ **那块 bit 不是由当前仓库这份 RTL 综合出来的**（至少 `audio_axis_core` 的 GPIO 输出不是）。这条直接后果：不能用它读采集计数，只能用来判断"手上这块 bit 是哪一版"。这也是为什么本文档的所有计数器读数都标注了"在哪块 bit 上测的"。

---

## 问题 6：Slice 利用率 99.40 %，没有余量

| 项 | 用量 | 占比 |
|---|---|---|
| Slice（打包后） | 13 220 / 13 300 | 99.40 % |
| Slice LUTs | 19 656 / 53 200 | 36.95 % |
| Slice Registers | 48 299 / 106 400 | 45.39 % |
| Block RAM | 8 / 140 | 5.71 % |

LUT 只用了 37 %，但**打包后的 Slice 已经 99.4 %**——说明布局被"每个 Slice 只放一到两个 LUT"撑满了，典型原因是那 3 万个触发器型 FIFO 挤占了 Slice 内的 FF 位置。修完问题 1 的第 2 条（AFIFO 进 BRAM）后，这一项应该会显著回落，届时要重测并更新 `metrics/metrics.csv`。

---

## 问题 7：`dac_split` 在归档 bit 里不存在

控制台「双 DAC」一栏显示 `dac_mode = 3`「未生效（这块 bit 里没有 dac_split）」。这不是 bug，是**诚实报错**：10-29 的 bit 早于 `dac_split` 的引入。要看这一路功能，需要用当前 RTL 重新构建——但先解决问题 1，否则新 bit 采集是 0 包。

---

## 复现这些问题（供评审复核）

```powershell
# 1. 看"0 包"证据：综合日志里的 AFIFO 警告
Select-String -Path fpga\report\audio_core_ooc_synth.log -Pattern "8-4767|8-13159|8-7137"

# 2. 看"不受约束"证据：no_clock 的 16923 个引脚
Select-String -Path fpga\report\timing_summary_routed.rpt -Pattern "no_clock|aud_bclk_reg" -Context 0,3

# 3. 看"PL 谱减没接"证据
Select-String -Path fpga\build\build_eth_audio.tcl,fpga\src\rtl\audio_axis_core.v -Pattern "axis_dn|dn_top"
# 期望：无输出

# 4. 看资源与时序
Select-String -Path fpga\report\utilization_placed.rpt -Pattern "Slice LUTs|Slice Registers|Block RAM Tile"
Select-String -Path fpga\report\timing_summary_routed.rpt -Pattern "WNS|WHS"
```
