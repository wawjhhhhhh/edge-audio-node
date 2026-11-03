# 系统架构与数据流

## 1. 总览

```
 ┌──────────────────────── Zynq-7020 xc7z020clg400-2 ─────────────────────────┐
 │  PL (fabric)                                          PS (双核 ARM A9)      │
 │                                                                             │
 MIC ─I2S─▶ audio_receive ─▶ audio_axis_core ─m_axis─▶ acc_rx ─▶ axi_dma_a ───▶│
 (ES8388)   (bclk 域解串)     (拼立体声字 + AFIFO)   (AXIS 时钟转换)  S2MM      │
 │                                  ▲                        │                  │
 │                                  │                        ▼                  │
 │                            u_tx_fifo ◀── acc_tx ◀──── axi_dma_mm2s  ◀── DDR │
 │                                  │                                    ▲     │
 │  aud_mclk ◀── clk_wiz_0 ◀── FCLK1                                     │     │
 │  ES8388 配置 ◀── audio_config(i2c_dri + i2c_reg_cfg) ── aud_scl/sda   │     │
 │                                                                       │     │
 │  axi_gpio ch1/ch2 ── 状态回读(0x41200000 / 0x41200008)                 │     │
 │  axi_ethernet (RGMII, YT8531C) ────────────────────────────▶ 100 Mbps ─┘     │
 └─────────────────────────────────────────────────────────────────────────────┘
        │ aud2_*（同一 I2S 总线并联到 40pin 排针）           ▲ 169.254.191.50
        ▼                                                   │
   外接 DAC ──▶ 耳机/音箱                        ┌──────────┴───────────┐
                                                 │ 电脑控制台 8770 (web) │
   ESP32-S3 ◀── TCP 9200 (PCM s16le 16 kHz) ─────┤  udp_audio / 大模型   │
        └──I2S──▶ MAX98357A ──▶ 喇叭              └──────────────────────┘
```

## 2. 三条数据流

### A. 采集上行（本作品的主链路）

1. `slave_signal_gen` 由 12.288 MHz MCLK 分频出 **LRCK 48 kHz**、**BCLK 1.536 MHz**（32 BCLK/帧，每声道 16 bit）。
2. `audio_receive` 在 BCLK 域把 `aud_adcdat` 串并转换，`rx_cnt == WL`（16）时给出 `rx_done` 与 16 bit 采样。
3. `audio_axis_core` 把左右声道拼成 32 bit（`{L16,R16}`），在 `rx_done` 且 `aud_lrc==1` 时 `rx_push` 一次，写入 `u_rx_fifo`（写侧 BCLK 域、读侧 50 MHz 域，格雷码指针 + 2FF 同步）。
4. 读侧输出 AXIS（`tvalid = ~fifo_empty`），经 `axis_clock_converter` 跨到 100 MHz，再进 `axi_dma_a` S2MM 写 DDR。
5. PS 侧 `udp_mic.c` 的帧回调每 10 ms 取走 480 word（1920 B），`app_ea_main.c` 按档位处理后，`udp_link.c` 封成两个 968 B 的 UDP 包（8 B 头 + 960 B 半帧）发往 `50505`。

### B. 回放下行（PC → 板子 → 模拟输出）

1. PC 把 PCM 发到 `50506`；PS 收进 4 个 100 ms 播放缓冲（乒乓），通过 DMA MM2S 写回 PL。
2. `u_tx_fifo` 在 50 MHz 侧写入、BCLK 侧读出，`always @(negedge aud_bclk)` 移位输出到 `aud_dacdat`。
3. `aud_dacdat` 同时扇出到板载 ES8388 与外接 DAC（`aud2_bclk/lrc/dacdat` 是同一总线的并联副本）。

### C. 控制与状态面

| 通道 | 方向 | 内容 |
|---|---|---|
| UDP `50507` 单字节 | PC → 板 | `'0'` 旁路 / `'1'` RNNoise / `'2'` 门限 / `'3'` PL 谱减 / `'a'..'d'` 底噪档 / `'t'` 测试音 |
| UDP `50508` 15 B | 板 → PC | 每 250 ms 一包状态（字段见 `interface.md`） |
| AXI GPIO ch1 `0x41200000` | 双向 | 板级控制位（PL 谱减旁路开关 bit0） |
| AXI GPIO ch2 `0x41200008` | 板 → PC | `{sda_low_cnt, scl_fall_cnt}`：codec I2C 健康度 |

> 关键设计：状态包里的每一项都是**板子自己报的数**，不是控制台"最后一次发出去的值"。控制台右侧状态栏标注「来源：板子回读」，就是为了让"点了没效果"这件事能立刻被发现。

## 3. 软硬件划分依据

| 放在 PL | 理由 |
|---|---|
| I2S 位级收发 | BCLK 1.536 MHz 的采样沿必须硬件对齐，软件无法保证 |
| 跨时钟域缓冲 | 12.288 MHz / 1.536 MHz / 50 MHz / 100 MHz 四个域，必须靠 FIFO + 格雷码 |
| 10 ms 定长成帧 | 帧间隔由硬件固定，抖动为 0，PC 端不必做重采样 |
| 码流搬运（DMA） | 1920 B/10 ms 持续搬运，零 CPU 参与 |

| 放在 PS（ARM） | 理由 |
|---|---|
| UDP 协议栈 | 自写精简栈（`udp_link.c`），不依赖 lwIP，帧时序可控 |
| 降噪三档 | RNNoise 需要查表 + 神经网络推理，PL 侧版本尚在打磨 |
| 播放缓冲与时钟校正 | 需要按 PC 侧速率动态丢/补帧 |

| 放在 PC | 理由 |
|---|---|
| 大模型语音助手 | 模型在电脑上跑，板子只需执行动作、出声 |
| 降噪 A/B 录音与界面 | 需要文件系统、波形绘制、参数面板 |

**这张表的代价是实测出来的**：RNNoise 在这颗 667 MHz 的 A9 上 **20.07 ms 处理不了 10 ms 的一帧**，即软件档根本跑不到 100 fps（当前用"一半帧直通"保证连续性）。这正是要把谱减搬进 PL 的直接原因——PL 版本 `dn_top` 用 512 点定点 FFT + 18 张增益表，流水后每帧只占几百个 200 MHz 周期。

## 4. PL 模块清单

| 文件 | 职责 | 关键参数 |
|---|---|---|
| `fpga/src/rtl/eth_audio_top.v` | 板级顶层：RGMII/MDIO IOBUF、UART、音频引脚扇出 | — |
| `fpga/src/rtl/audio_axis_core.v` | 音频核顶层：时钟、I2S、FIFO、AXIS | `WL=16 BEATS_PER_PKT=480 AW=9 LOOPBACK=0` |
| `fpga/src/rtl/slave_signal_gen.v` | 由 MCLK 产生 LRCK/BCLK | `FREQ_DIV=256` → LRCK 48 kHz、BCLK 1.536 MHz |
| `fpga/src/rtl/audio_receive.v` | I2S 串并转换、声道对齐 | `WL=16` |
| `fpga/src/rtl/afifo.v` | 异步 FIFO（格雷码指针，2FF 同步） | `DW=32`，`AW=9`（512 深） |
| `fpga/src/rtl/audio_config.v` | ES8388 上电配置（延时 + I2C 写寄存器表） | 20 bit 延时约 21 ms |
| `fpga/src/rtl/i2c_dri.v` / `i2c_reg_cfg.v` | I2C 主机与寄存器序列 | 100 kHz |
| `fpga/src/rtl/dac_split.v` | 双 DAC 左右声道门控（当前未接进数据通路） | — |
| `fpga/src/rtl/eth_txctrl_gen.v` | 以太网 TX 控制字生成 | — |
| `fpga/src/denoise/axis_dn.v` | AXIS 包裹的谱减核（未接入 BD） | 512 点 |
| `fpga/src/denoise/dn_top.v` | FFT → 幅度/相位 → 增益表 → IFFT | 18 张表 |
| `fpga/src/denoise/fft512.v` | 定点 512 点 FFT（基 2，流水） | 16 bit |
| `fpga/src/denoise/dn_gain.v` / `dn_rom.v` | 谱减增益计算与查表 | — |

## 5. 时钟域

| 时钟 | 频率 | 来源 | 谁在用 |
|---|---|---|---|
| `clk_fpga_0` | 100 MHz | PS FCLK0 | DMA、AXIS 读侧、以太网 |
| `clk_fpga_1` | 50 MHz | PS FCLK1 | audio_axis_core 主时钟 |
| `clk_wiz_out1` | 125 MHz | PL MMCM | （以太网侧） |
| `clk_wiz_out2` | 200 MHz | PL MMCM | （以太网侧） |
| `aud_mclk` | 12.288 MHz | PL MMCM（=256×48 kHz） | I2S 时序产生 |
| `aud_bclk` | 1.536 MHz | `aud_mclk/8` | I2S 收发、FIFO 写侧 |
| `aud_lrc` | 48 kHz | `aud_mclk/256` | 帧同步（50% 占空比） |

> ⚠️ `aud_bclk` / `aud_lrc` 目前**没有** `create_clock`，16923 个触发器因此不受时序分析——这是 `known_issues.md` 里"重建 0 包"的根因之一。

## 6. 资源与实现结果（impl_1，post-placement / routed）

| 项 | 用量 | 占比 |
|---|---|---|
| Slice LUTs | 19 656 | 36.95 % |
| Slice Registers | 48 299 | 45.39 % |
| Block RAM Tile | 8 | 5.71 % |
| DSP48E1 | 0 | 0 % |
| BUFGCTRL | 11 | 34.38 % |
| Slice（打包） | 13 220 / 13 300 | **99.40 %** |
| WNS（受约束时钟） | +0.357 ns | 0 failing endpoint |
| WHS | +0.024 ns | 0 failing endpoint |
| 布线 | 58 015/58 015 routable nets | 0 error |

原始报告在 `fpga/report/`。

## 7. 没有接进数据通路的部分

`fpga/src/denoise/` 是一套完整的 PL 谱减实现（FFT-512 → 幅度平滑 → 增益表 → IFFT），配有 8 个 testbench 和 Python 定点黄金模型（`model.py` / `dn_gain_model.py`），但**当前 BD 里没有实例化 `axis_dn`**：`build_eth_audio.tcl` 的数据通路仍是 `audio_axis_core → acc_rx → axi_dma_a`。

它现在的状态是"能过仿真、与黄金模型逐点对齐"，接入需要三件事：
1. 在 BD 里把 `axis_dn` 插在 `audio_axis_core/m_axis` 与 `acc_rx/S_AXIS` 之间（两侧同为 50 MHz，不需要额外 CDC）；
2. 把 GPIO `0x41200000` bit0 接到 `axis_dn/bypass`，让控制台的 `'3'` 档真正生效；
3. 用 `ab_pl.py` 做同源 A/B（同一段噪声进旁路与谱减两路），量化 SNR 与 THD。

这三步的验收口径写在 `docs/known_issues.md` 第 4 节。
