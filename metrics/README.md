# 指标测量说明

## 1. 文件组织

```
metrics/
├── metrics.csv         # 指标主体（表头按赛题 3.2.5.2 固定）
├── README.md           # 本文件：怎么测、怎么复测、哪些没测到
├── logs/               # 原始日志与抄录
│   ├── console_fps_20261103.csv     # 帧率/丢包/欠载读数
│   ├── audio_core_ooc_synth.log     # 音频核 OOC 综合日志（RNNoise 与 FIFO 警告）
│   ├── timing_axis.log              # 实现时序日志
│   ├── program_axis.log             # 下载与运行日志
│   └── web_server_run.log           # 控制台运行日志
├── scripts/            # 可重跑脚本
│   ├── sample_console_state.py      # ★ 帧率/丢包/DBFS 采样（写 CSV）
│   ├── chk_link_now.py              # 链路是否通
│   ├── udp_probe.py                 # 裸 UDP 探包（不经控制台）
│   ├── latency_test.py              # 端到端延迟（需示波器）
│   ├── bench_denoise.py             # 降噪效果基准（PS 侧）
│   ├── bench_playfx.py              # 播放音效基准
│   ├── ab_pl.py                     # PL 谱减同源 A/B（待接入后启用）
│   └── nz_board_verify.py           # 降噪档位板级验证
└── evidence/           # 截图证据
```

## 2. 类别定义

| 类别 | 含义 |
|---|---|
| 实测 | 在真实板子上跑出来、有日志或截图佐证的数字 |
| 计算 | 由实测值推导（如吞吐 = 包数 × 包长 × 8），公式写进"测量条件" |
| 未测 | 客观上没测成。**不填估算值**，把缺什么条件写清楚 |

`metrics.csv` 里有两行是"未测"：端到端延迟（缺双通道示波器）、PL 谱减的 SNR 提升（核没接进 BD）。这两项宁可空着也不编。

## 3. 测量条件为什么写这么长

赛题要求测量条件写清 **硬件型号、模型与精度、输入规格、软件版本、FPGA 频率与功耗模式**。本工程的条件写法：

```
ZYNQ-7020 xc7z020clg400-2；clk_fpga_0=100MHz；48kHz/16bit 立体声 1920B/帧；
板载 ES8388；固件 app_ea.elf(633976B, 10DD461D1834)；
比特流 eth_audio_top_20261029.bit(931DA562B98C)；PC 169.254.191.246 直连
```

其中**比特流与固件的哈希必须写进条件里**，因为这个工程有过"同一份 RTL 重新实现就 0 包"的历史（见 `docs/known_issues.md` 问题 1）——不写哈希，指标就没有可追溯性。

## 4. 复测步骤

```powershell
# 0) 前置：板子在线（ping 通）、控制台已起在 8770
ping 169.254.191.50

# 1) 60 秒采样，写 CSV（默认 120 点，每 0.5 s 一点）
python metrics\scripts\sample_console_state.py --seconds 60 --out metrics\logs\fps_recheck.csv

# 2) 只看链路
python metrics\scripts\chk_link_now.py

# 3) 不经控制台的裸探包（证明不是网页的问题）
python metrics\scripts\udp_probe.py

# 4) 资源与时序（重新实现后必做）
vivado -mode batch -source fpga\build\build_eth_audio.tcl
Select-String -Path fpga\report\utilization_placed.rpt -Pattern "Slice LUTs|Slice Registers|Block RAM Tile"
```

## 5. 已知的测量局限（必须跟评审说明）

1. **打包时板子不在线**（`/api/state` 为 `link=offline`），所以帧率类指标用的是 2026-11-03 20:2x 的抄录读数，不是当场复测。复测脚本已随包提供，板子回来后一条命令就能重出数据。
2. **帧率读数是控制台 6 秒窗口的均值**，不是长时间统计。短窗口对"是否 100 fps"够用，对抖动评估不够。
3. **功耗是 Vivado 估算值**（`power_routed.rpt`，25℃ 典型角、12.5% 翻转率），不是实测功率。实测需要电流探头或板载功率监测。
4. **RNNoise 的 20.07 ms/帧** 是 PS 侧串口打印的时间差，含半帧直通策略的影响；它说明"软件档跑不到 100 fps"，不作为算法本身的性能结论。
5. **两个"未测"项**见第 2 节。

## 6. 数字一致性检查

写报告前先对一遍这几个数，全部来自本目录：

| 数 | 值 | 出处 |
|---|---|---|
| 帧率 | 100.0 fps | `logs/console_fps_20261103.csv` |
| 帧长 | 1920 B（480 word） | `docs/interface.md` §2 与 `host/firmware/app_ea_main.c:67-74` |
| 包长 | 968 B（8 B 头 + 960 B 数据） | 同上 |
| 包率 | 200 包/秒 | 同上 |
| 吞吐 | 1.549 Mbit/s | 本目录 metrics.csv（计算行） |
| 资源 | LUT 19 656 / FF 48 299 / BRAM 8 / DSP 0 | `fpga/report/utilization_placed.rpt` |
| Slice | 13 220/13 300 = 99.40 % | 同上 |
| WNS | +0.357 ns | `fpga/report/timing_summary_routed.rpt` |
| 功耗 | 2.189 W（动态 2.031 / 静态 0.158） | `fpga/report/power_routed.rpt` |
