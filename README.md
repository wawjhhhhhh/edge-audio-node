# edge-audio-node — 端侧实时音频链路与语音交互系统

> 作品名称：**AI 解码器**
> 参赛赛道：全国大学生嵌入式芯片与系统设计竞赛 2026 · FPGA 赛道 · 自主选题（高级组）
> 平台：Zynq-7020（xc7z020clg400-2）+ 电脑端控制台 + ESP32-S3 无线放音

一句话：麦克风信号进入 Zynq 的 PL，经片内异步 FIFO 与 AXIS/DMA 以 **100 帧/秒**送到电脑；电脑端做降噪 A/B 对比与大模型语音助手，声音再经 ESP32 无线送到喇叭。**整条链路可现场重跑、可逐级观测**。

---

## 1. 解决什么问题

现场扩声、课堂录制、远程会议的共同问题是：麦克风离说话人远，**底噪和房间混响压过语音**。要把它做好，需要同时满足三件互相冲突的事：

| 需求 | 冲突点 | 本作品的做法 |
|---|---|---|
| 实时（不能有抖动、不能断） | 软件降噪一卡顿就爆音 | PL 负责采样、跨时钟、搬运；帧间隔由硬件固定为 10 ms |
| 当场能对比 | 只给"处理后"，没法证明有效 | 一档一键切换 + 双轨录音（`metrics/evidence/`） |
| 操作要能用嘴说 | 面板参数多，现场不方便 | 大模型语音助手：一句话改参数，并让板子喇叭回话 |

## 2. 四个可演示能力

1. **实时采集上行**：48 kHz / 16 bit / 立体声，每 10 ms 一帧（480 word = 1920 B），拆成两个 968 B 的 UDP 包发往 `50505`，实测 **100.0 fps**。
2. **三种降噪档位 + A/B**：旁路 / 轻量噪声门 / RNNoise，一键切换；控制台的「录音与 A/B」把两档各录一遍，直接对听。
3. **双 DAC 输出**：板载 ES8388 与外接 DAC 同时出声，可对比两个模拟通路（`fpga/src/rtl/dac_split.v`）。
4. **语音助手**：在控制台打字（或说话），大模型理解后调用板子控制接口（改模式 / 增益 / 时长比例 / 底噪），并用语音从 ESP32 喇叭答出来。

## 3. 硬件组成

| 部件 | 型号 | 作用 |
|---|---|---|
| 主控板 | 正点原子 ATK-DFPZ20，Zynq-7020 `xc7z020clg400-2` | PS(ARM A9 ×2) + PL(85K LC)，跑整条采集/搬运算力通路 |
| 板载 codec | ES8388（I2C 配置，I2S 音频） | 麦克风输入 + 耳机输出 |
| 外接 DAC | I2S 从机（`aud2_*` 第二组 I2S） | 第二路模拟输出，与板载 codec 并行 |
| 无线放音 | ESP32-S3-WROOM N16R8 + MAX98357A | 收 PC 的 PCM（TCP 9200），I2S 直推喇叭 |
| 上位机 | Ryzen 笔记本 + 千兆网口（169.254.191.246） | 控制台、降噪对比、大模型助手 |

接线、引脚、供电见 [`docs/hardware_setup.md`](docs/hardware_setup.md)。

## 4. 目录结构与赛道推荐结构的对照

本仓库按《选题指南》**3.2.5.5 推荐目录结构**组织（工程包 + 指标报告 + Skill + 设计报告四段式），并给出与 3.3.5.4（自主选题赛道）结构的对照，二者可互查：

| 3.2.5.5（本仓库采用） | 内容 | 3.3.5.4 对应 |
|---|---|---|
| `docs/` | 架构、接口、硬件、已知问题 | `README.md` + `docs/` |
| `fpga/src/` | RTL（采集/回放/跨时钟/降噪）与 testbench | `src/` + `sim/` |
| `fpga/build/` | 构建脚本、约束、产物 `.bit`/`.xsa`/`.hwh` | `build/` + `board/` |
| `fpga/report/` | 综合/实现/时序/功耗报告 | `build/` |
| `host/app/` | 电脑端控制台（Python）+ 语音助手 + ESP32 端程序 | `src/` |
| `host/firmware/` | PS 侧固件（裸机 UDP/降噪） | `src/` |
| `host/model/` | 模型来源与版本说明 | `data/` |
| `metrics/` | 指标汇总表 + 原始日志 + 测试脚本 + 证据 | `data/` |
| `skill/` | 技能包（可复用工作流/模板/校验脚本/踩坑清单） | `skill/` |
| `report/` | 设计报告（Word） | `report/` |
| `poster/` | 会议海报（中/英两版，按组委会模板生成） | `report/` |

## 5. 复现步骤

> 全部命令在仓库根目录执行。硬件型号、引脚、IP 见对应文档。

**① 环境**
- Vivado / Vitis 2026.1（Zynq-7000 器件支持）
- Python 3.12，依赖：`pip install -r host/deploy/requirements.txt`
- 电脑与板子同网段（板子固定 `169.254.191.50`，电脑 `169.254.191.246`）

**② 构建 PL**
```tcl
source fpga/build/build_eth_audio.tcl
```
产出 `eth_audio_top.bit`、`eth_audio.xsa`、`eth_audio.hwh`。构建脚本会重写约束文件、校验 `LOOPBACK=0`、WNS<0 直接报错。

**③ 构建 PS（可以直接跳过）**

包内已带编译好、板上验证过的固件 `fpga/build/output/app_ea_fpu.elf`（633 976 B，SHA256 前缀 `10DD461D1834`），不改固件就不用编译。要改才需要：

```bash
python fpga/build/build_app_ea.py   # ⚠️ 脚本里是原始开发机的绝对路径，换机器要改
```

详细步骤（Vitis 组件、RNNoise 库、编译选项）见 [`host/firmware/README_firmware.md`](host/firmware/README_firmware.md)。

**④ 上板**
```bash
# 烧 bit + 下载 ELF（xsdb）；注意先核对脚本里烧的是哪个 bit
xsdb fpga/build/flash_app_ea.tcl
```

**⑤ 起控制台**
```bash
python host/app/run_web.py --no-window      # 浏览器打开 http://127.0.0.1:8770
# 或者双击：host\deploy\run_console.cmd
```

验收：控制台右上角 `板子在线 · 100 fps`；「实时」页波形有电平（DBFS 约 −30 ~ −50）；点「录音与 A/B」得到两段 wav。

自检：

```bash
python host\app\selftest.py                 # 控制台与网络（26 项，不需要板子）
python skill\scripts\check_delivery.py .    # 交付包结构
python skill\scripts\check_timing_health.py # 时序报告健康度
```

语音助手工具链的自检要用模块方式跑（`xiaozhi` 包内是相对导入）：

```bash
cd host\app
python -m xiaozhi.run --selftest            # 16 项：PASS 13 / SKIP 3 / FAIL 0
```

SKIP 的三项是 ESP32 串口（没插板）与 ASR 模型（未下载），不影响其它项。

## 6. 关键指标（详见 `metrics/metrics.csv`）

| 指标 | 数值 | 条件 |
|---|---|---|
| 上行帧率 | 100.0 fps | 48 kHz/16 bit 立体声，1920 B/帧 |
| 上行带宽 | 1.54 Mbit/s | 同上，200 包/秒 × 968 B |
| 采集→控制台丢包 | 0（10 s 观测） | 直连百兆/千兆 |
| PL 时序（受约束时钟） | WNS +0.357 ns | 100/125/200 MHz 域，0 failing endpoint |
| 资源占用 | LUT 19 656（36.9%）、FF 48 299（45.4%）、BRAM 8（5.7%）、DSP 0 | impl_1 post-placement |

⚠️ 有两项必须一起读：**Slice 打包率 99.40%**，以及 **aud_bclk 域尚未加时钟约束**（16 923 个引脚 `no_clock`）。这两件事直接导致"重新综合后的 bit 采集为 0 包"，原因与修复方案写在 [`docs/known_issues.md`](docs/known_issues.md)。

## 7. 现在能做什么、还不能做什么

**已经能稳定演示**：上板采集 100 fps、三档降噪切换、双 DAC、控制台全部页面、语音助手（打字 → 大模型 → 调参 → 喇叭回话）。

**已写好但没接进数据通路**：PL 侧谱减降噪（`fpga/src/denoise/`，FFT-512 + 18 张增益表 + 逆变换，配 8 个 testbench 与 Python 黄金模型）。它当前是"能仿真、未上板"的状态，接入点与验收口径见 `docs/known_issues.md` 第 4 节。

**还没解决**：重新综合 + 实现的 bit 采集 0 包（用归档的 2026-10-29 bit 可正常演示）。这是本作品当前第一优先级的问题，证据链与四条修复措施都写在 `docs/known_issues.md`。

## 7.1 智能体协作：用过什么、踩过什么

本作品全程由"人 + 智能体"协作完成，智能体侧用的是 **AMD Ross™ Agentic AI Assistant**（MCP 服务直接驱动 Vivado / Vitis / xsdb，配 AMD 知识库与 agent skills），客户端是可替换的。

用它的收益很直接：读 28 页赛题 PDF、遍历 RTL、写约束、跑构建、解析报告、生成文档，这些成倍省时间。但它**会给错结论**，而且错得很自信——本工程最典型的两次是：

1. 按端口注释把 I2S 位时钟写成 3.072 MHz（真实值 1.536 MHz，源码里就是 `bclk_div_cnt == 8'd3`）；
2. 综合报告"时序全过"，但一整个 16 923 触发器的时钟域根本没加约束，于是重建出来的 bit 采集是 0 包。

把这些坑、根因和规避动作逐条记了下来：**[`docs/amd_ross_agent_notes.md`](docs/amd_ross_agent_notes.md)**（20 条，每条带本仓库内的证据文件）。其中被验证可复用的 5 条已提炼成 `skill/` 里的 S1–S5。

## 8. 目录速查

```
edge-audio-node/
├── README.md                      本文件
├── docs/                          架构 / 接口 / 硬件 / 已知问题 / 智能体协作笔记 / 交付清单(manifest.tsv)
├── fpga/
│   ├── src/rtl/                   采集、回放、跨时钟 FIFO、codec 配置、双 DAC
│   ├── src/denoise/               PL 谱减降噪 RTL + 定点生成脚本 + 黄金模型
│   ├── src/tb/                    testbench 与定点向量
│   ├── build/                     构建/烧写脚本、约束、产物（.bit/.xsa/.hwh/.elf）
│   └── report/                    综合、实现、时序、功耗、方法论 DRC 报告
├── host/
│   ├── app/                       电脑端控制台（web_server / udp_audio / web 前端）
│   ├── app/xiaozhi/               大模型语音助手 + ESP32 端 MicroPython 程序
│   ├── firmware/                  PS 侧裸机固件 + 已验证 elf 说明 + RNNoise 头文件
│   ├── model/                     模型来源、精度、后端（含 Vosk 下载方式）
│   └── deploy/                    依赖清单 requirements.txt 与一键启动脚本
├── metrics/                       metrics.csv + README + logs + scripts + evidence
├── skill/                         技能包（README + 两个自检脚本）
└── report/                        设计报告（design_report.docx / .md + figures）
```

## 9. 许可与致谢

- 本项目代码以 MIT 协议开源；第三方组件（RNNoise、Vosk、edge-tts、MicroPython）遵循各自协议，见 `report/design_report.md` 参考文献。
- 感谢实验室提供的示波器、逻辑分析仪与焊接工位。
