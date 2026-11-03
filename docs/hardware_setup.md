# 硬件与接线

本文件回答三件事：**买了什么**、**怎么接**、**复现时要按什么顺序上电**。所有引脚号都来自 `fpga/build/eth_audio_pins.xdc` 与 `fpga/src/rtl/eth_audio_top.v`，不是从手册抄的。

## 1. 物料清单（BOM）

| # | 名称 | 型号 / 规格 | 数量 | 作用 | 是否必需 |
|---|---|---|---|---|---|
| 1 | 主控开发板 | 正点原子 ZYNQ-7020（`xc7z020clg400-2`） | 1 | PS + PL 全部逻辑与协议 | 必需 |
| 2 | 板载音频 Codec | ES8388（板载，I2C 地址 `0x10`） | 1 | 麦克风采集 + 耳机输出 | 必需 |
| 3 | 外接 DAC 模块 | 并联 I2S 从机（走 40pin 排针 `aud2_*`） | 1 | 第二路模拟输出，做"双 DAC"对照 | 可选 |
| 4 | 模拟麦克风模块 | 帯增益电位器的驻极体放大模块，接 ES8388 MIC 输入 | 1 | 采集端声源 | 必需 |
| 5 | ESP32-S3 开发板 | ESP32-S3-WROOM-1 N16R8，MicroPython v1.29.0 | 1 | 接收 PCM 并驱动喇叭 | 可选 |
| 6 | 数字功放 | MAX98357A（I2S 输入） | 1 | 驱动喇叭 | 可选 |
| 7 | 喇叭 | 4–8 Ω，3 W 以内 | 1 | 听降噪后的声音 | 可选 |
| 8 | 网线 | 直连 PC（静态 IP 同网段） | 1 | 100 Mbps 链路 | 必需 |
| 9 | USB 线 | Micro-USB ×2（JTAG/UART + 供电） | 2 | 下载与调试 | 必需 |
| 10 | 耳机 | 3.5 mm | 1 | 板载 codec 输出 | 可选 |

> 麦克风模块的具体型号以实物丝印为准，本包不对未核实的型号做断言；只要输出模拟信号并能进 ES8388 的 MIC 通道即可。

## 2. 主控板与供电

- 供电：DC 5 V / 2 A 适配器，或 USB 供电（USB 供电时 PL 侧大电流外设可能欠压，建议用适配器）。
- 上电顺序：**先接好所有杜邦线 → 再上电 → 最后插 USB 下载线**。反过来容易在 PL 配置未完成时被外部器件拉低。
- 板上三颗 LED：`led0`（H15）常亮表示 PS 已启动；`led1`（L15）以 1.365 s 周期心跳表示 PL 逻辑在跑。
- 复位：`PS-RST` 按键复位整个系统；只复位 PL 请用 Vivado Hardware Manager 重新下载 `.bit`。

## 3. 音频接口（板载 ES8388，主通路）

| 信号 | FPGA 引脚 | 方向（相对 FPGA） | 说明 |
|---|---|---|---|
| `aud_mclk` | E19 | 输出 | 12.288 MHz 主时钟 |
| `aud_bclk` | M18 | 输出 | 1.536 MHz 位时钟（= MCLK/8） |
| `aud_lrc` | L20 | 输出 | 48 kHz 帧同步，低 = 左声道 |
| `aud_dacdat` | G17 | 输出 | 送往 codec 的 PCM |
| `aud_adcdat` | M17 | 输入 | 来自 codec 的 PCM |
| `aud_scl` | E18 | 输出 | I2C 时钟（100 kHz） |
| `aud_sda` | F17 | 双向 | I2C 数据 |

codec 由 PL 在复位后约 21 ms 通过 I2C 写完寄存器表（`audio_config.v` + `es9038_cfg.v`），**不需要 PS 参与**。若听不到声音，先读 `0x41200008` 看 `scl_fall_cnt` 是否在增长——那说明 I2C 在动。

## 4. 第二路 I2S（40pin 排针，外接 DAC）

| 信号 | FPGA 引脚 | 说明 |
|---|---|---|
| `aud2_mclk` | W10 | 与 `aud_mclk` 同源（**必须与主 audio 时钟同组约束**） |
| `aud2_bclk` | W11 | 与 `aud_bclk` 并联 |
| `aud2_lrc` | Y13 | 与 `aud_lrc` 并联 |
| `aud2_dacdat` | Y11 | 与 `aud_dacdat` 并联 |
| `aud2_dacdat_w9` | W9 | 同上（同一网络的第二个引脚，为布线方便） |
| `aud2_dacdat_w8` | W8 | 同上 |

外接 DAC 与板载 ES8388 **共用一路 I2S**，因此两者放的是同一个声音——这正是"双 DAC 同时出声"的实现方式。接线时只需把 `aud2_bclk / aud2_lrc / aud2_dacdat` 三根线接到 DAC 模块，`aud2_mclk` 按模块要求决定是否接。

> 约束提醒：`aud2_mclk` 与 `aud_mclk` 必须落在同一个 `set_clock_groups` 组里。曾经因为构建脚本重写 XDC 时漏了 `aud2_mclk_OBUF`，导致 `clk_fpga_1 → aud2_mclk` 的复位释放被当成真实时序路径检查（15 个端点、WNS −2.858 ns），I2S 域起不来。

## 5. 以太网

| 信号组 | FPGA 引脚 |
|---|---|
| RGMII TX | K17 (TXC), E17 (TXD0), B19 (TXD1), A20 (TXD2), H17 (TXD3) |
| RGMII RX | H16 (RXC), B20 (RXD0), K18 (RXD1), D18 (RXD2), C20 (RXD3) |
| 控制 | D19 (RXDV/CTL), D20 (TXEN/CTL) |
| MDC / MDIO | F20 / F19 |
| PHY 复位 | G15 (`eth_rst_n`) |

PHY 为板载 YT8531C，自协商到 100 Mbps 全双工。PC 侧需设静态 IP `169.254.191.246/16`（与板子 `169.254.191.50` 同网段），并**关闭该网卡的防火墙或放行 UDP 50505–50508**。

## 6. 串口调试

| 信号 | FPGA 引脚 | 参数 |
|---|---|---|
| UART TX | J15 | 115200 8N1 |
| UART RX | T19 | 同上 |

不插网线时可用串口发单字节 `'0'..'3'` 切换降噪档、`'t'` 开关测试音。

## 7. ESP32-S3 与功放（语音输出支路）

| ESP32 引脚 | 接到 | 说明 |
|---|---|---|
| GPIO4 | MAX98357A `BCLK` | I2S 位时钟 |
| GPIO5 | MAX98357A `LRC`/`WS` | 帧同步 |
| GPIO6 | MAX98357A `DIN` | 数据 |
| 3V3 / GND | 功放 VIN / GND | 功放另接 5 V 更响 |

ESP32 通过 Wi-Fi 连热点（默认 SSID `8848`），开 TCP `9200` 收裸 PCM（16 kHz/16 bit/单声道），再经 I2S 推给功放。电脑端把地址填成 ESP32 的实际 IP 即可，见 `docs/interface.md` 第 8 节。

## 8. 复现步骤（硬件侧）

1. 按上表接好 I2S、I2C、网线、串口；**先不接外接 DAC 与 ESP32**，把主通路跑通。
2. 上电，`led0` 常亮。
3. JTAG 下载 `fpga/build/output/eth_audio_top_20261029.bit`（这是已验证可用的构建；原因见 `docs/known_issues.md`）。
4. 下载 PS 程序（`host/firmware/` 编出的 `app_ea.elf`），PC 设静态 IP 后 ping `169.254.191.50`。
5. 起控制台 `python host/app/run_web.py --no-window`，浏览器开 `http://127.0.0.1:8770`，点「开始接收」，应看到 `fps ≈ 100`、`DBFS` 随说话变化。
6. 通了之后再接外接 DAC（听第二路输出）与 ESP32（听无线喇叭）。

## 9. 硬件常见问题

| 现象 | 先查什么 |
|---|---|
| `led1` 不闪 | 比特流没下进去，或 PL 时钟没起来（`clk_wiz` 锁定） |
| `fps = 0` 但网口灯在闪 | 板子在发包但 PC 收不到：防火墙 / 网卡不是 169.254.191.246 |
| 有帧但全是噪声 | ES8388 没配置成功：读 `0x41200008`，`scl_fall_cnt` 不动就是 I2C 挂了 |
| 播放有杂音/断续 | PC 侧缓冲欠载，看控制台 `under` 计数；或 ESP32 与 PC 不同网段 |
| 外接 DAC 无声但板载有 | `aud2_*` 三根线接触不良，或该 DAC 需要 MCLK（接 W10） |
