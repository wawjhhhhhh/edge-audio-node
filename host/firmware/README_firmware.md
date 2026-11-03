# PS 侧固件：源文件、构建与已验证产物

## 1. 文件说明

| 文件 | 行数 | 作用 |
|---|---|---|
| `app_ea_main.c` | 约 2900 | 主循环：收包、档位切换、降噪调度、状态上报、UART 命令 |
| `udp_link.c` | 约 1500 | 自写以太网/IP/UDP 收发（不依赖 lwIP） |
| `udp_mic.c` | 约 300 | DMA 帧回调 → 上行组包 |
| `time_scale.h` | — | 时长缩放（语速不改变音高） |
| `vocoder.h` | — | 变声/音效 |
| `lscript.ld` | — | 链接脚本 |
| `third_party/rnnoise/rnnoise.h` | — | RNNoise 接口（库需自行编译，见 `host/model/README.md` §4） |

## 2. 不用编译也能跑

`fpga/build/output/app_ea_fpu.elf` 是编译好、在板上验证过的固件：

| 项 | 值 |
|---|---|
| 大小 | 633 976 B |
| SHA256 前缀 | `10DD461D1834` |
| 编译环境 | Vitis 2026.1，`-mfpu=neon`（FPU 版） |
| 配套比特流 | `fpga/build/output/eth_audio_top_20261029.bit` |

烧写顺序：先 `fpga -file` 下比特流，再 `dow` 这个 elf，最后 `con`。脚本见 `fpga/build/flash_app_ea.tcl`。

## 3. 要改固件时怎么编

⚠️ `fpga/build/build_app_ea.py` 里是**原始开发机的绝对路径**（`D:\vitis_tmp\ws_ea`），换机器必须改。步骤：

1. Vitis 2026.1 新建 workspace，新建 **Empty Application (C)**，platform 选 `eth_audio.xsa`（PS7 + UART + 定时器 + 中断）。
2. 把本目录下 `*.c` / `*.h` 加进 `src/`；`third_party/rnnoise` 加到 include 路径。
3. 链接 `librnnoise.a`（自己编，见 `host/model/README.md` §4）；若要 FPU 版，编译选项加 `-mfpu=neon -mfloat-abi=hard`。
4. Build → 得到 `app_ea.elf`。
5. 用 `fpga/build/flash_app_ea.tcl` 下载，或 `xsdb` 手动 `dow`。

## 4. 验证固件真的跑起来了（不看屏幕也能判断）

```powershell
# 1) 板子在线且 100 fps
python metrics\scripts\chk_link_now.py

# 2) 串口有输出（115200）
#    正常会看到启动横幅与周期性的统计行

# 3) 状态包在滚（15 B，250 ms 一包）
python metrics\scripts\udp_probe.py
```

三条都过，说明"固件 → DMA → 网络"这条链是通的；此时若 `fps=0`，问题在 PL 采集侧，见 `docs/known_issues.md` 问题 1。

## 5. 已知的坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `dac_mode` 一直是 3 | 比特流里没有 `dac_split`（10-29 的 bit 早于它） | 属正常；要这功能得重新构建 PL，但先解决问题 1 |
| RNNoise 档只有一半帧在降噪 | 单帧 20.07 ms > 10 ms，跑不满 | 见 `docs/known_issues.md` 问题 3 |
| `res[]` 调试寄存器不变 | 新固件不再更新它 | 改看 UDP 50508 状态包 |
