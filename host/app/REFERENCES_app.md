# 同类开源项目对标：PC 端音频监听应用该按什么顺序长

目的：不要自己摸黑发明顺序，先看别人从「能听」到「能交付」是怎么一步步长出来的，只借结构不借代码。

## 1. 五个值得抄结构、不值得抄代码的项目

| 项目 | 它是什么 | 它走过的顺序（关键） | 我们借什么 |
| --- | --- | --- | --- |
| [snapcast](https://github.com/badaix/snapcast) | 多房间同步音频（C++） | UDP 收包 → 固定缓冲 → **时间同步（NTP 式）** → **latency 调整：按误差丢/插样本，逐步收敛** → 重采样 | 缓冲深度不是固定值，而是"目标 + 缓慢纠偏"；我们已有的 ppm 纠偏就是这个思路的简化版 |
| [SonoBus](https://github.com/sonosaurus/sonobus) | 局域网实时合奏（C++/JUCE） | 点对点 UDP → 固定缓冲 → **自适应抖动缓冲** → 丢包隐藏 → 可选 FEC | "自动模式"：按实测抖动在低延迟/高稳定两档之间切，而不是让用户调一个数字 |
| [JackTrip](https://github.com/jacktrip/jacktrip) | 极低延迟远程演出 | 固定极小缓冲（靠网络质量） → 加 PLC → 加多客户端 | 低延迟档的下限该定在哪（我们 60 ms 已经接近这类工具的默认值） |
| [WebRTC NetEq](https://android.googleid.googlesource.com/platform/external/webrtc/+/7103e7085fdb146af8a7878a6dc0615b9b800deb/modules/audio_coding/neteq/g3doc/index.md)（[merge.c](https://android.git.googlesource.com/platform/external/webrtc_legacy/+/470e71d3649f6cac4688e83819640b012b5d38bb/src/modules/audio_coding/NetEQ/main/source/merge.c)） | 浏览器语音的丢包对策教科书 | expand（外推）→ merge（拼接）→ accelerate（加速播放）三级降级 | 丢帧隐藏从"淡出淡入"升级到"基音周期外推"的理论依据与降级层次 |
| [pyqtgraph-spectrographer](https://github.com/remusao/pyqtgraph-spectrographer) | 最小可用频谱程序（Python + pyaudio） | 单线程读声卡 → 画频谱 → 加瀑布图 → 加设备/参数面板 | 频谱 UI 的极简骨架；证明"每帧只改坐标、不重建图元"是标准做法（我们已这么做） |

## 2. 我们已经踩在他们老路上的点（说明方向没错）

- 自适应缓冲目标：snapcast / SonoBus 都有，我们按 `60 + 4×jitter` 实现，带死区与限速。
- 漂移纠偏：snapcast 的 latency 调整 = 我们的 ppm 纠偏（每秒最多 1 样本）。
- 丢帧处理：我们现在是淡出淡入（NetEq 的最初级），他们还有 expand / merge。
- 频谱 UI：pyqtgraph-spectrographer 的做法 = 我们的 Canvas coords 方案。

## 3. 按"投入产出"排的采纳顺序（每项 ≤2 小时，不引入依赖）

1. **自动档位（学 SonoBus）**：界面放「低延迟 / 稳定」两档，低延迟目标 60 ms、稳定目标 150 ms，切换时用现有自适应逻辑平滑过去。演示价值最高：网络抖的时候点一下就不卡。
2. **丢包外推（学 NetEq 的 expand）**：丢帧时不用"上一帧淡出"，改用上一帧最后两个基音周期（自相关在 40–400 Hz 找周期）重复填充。听感从"咔"变"糊"，比静音好。
3. **播放全链路记录**（学 snapcast 的诊断思路）：一键导出「收到 / 播放 / 丢弃」三路统计 CSV，答辩时直接画图。
4. **多客户端**（学 snapcast）：同一路流给两台电脑同步放 —— 答辩加分项，成本高，放最后。
5. **重采样**（学 snapcast）：只在 PC 声卡不是 48 kHz 时才需要；现在声卡是 48 kHz，暂不做。

## 4. 明确不学的部分

- **Opus / WebRTC 全套**：局域网 48 kHz 裸 PCM 只有 1.5 Mbps，压缩是我们不需要的复杂度。
- **FEC / 重传**：我们有 100 fps + 200 ms 缓冲，丢包率实测 0，先不做。
- **时钟同步协议**：只有一台 PC，没有多端同步需求。

## 5. 结论

我们的顺序（协议 → 缓冲 → 稳定性 → 可视化 → 控制面）和他们一致；差的是「自适应档位」和「外推式丢包隐藏」这两步，属于锦上添花而非地基。接下来只做第 3 节的 1、2 两项，其余写进"下一步"。
