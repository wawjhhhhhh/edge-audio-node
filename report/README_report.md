# 设计报告（report 目录说明）

## 交付物

| 文件 | 说明 |
| --- | --- |
| `design_report.docx` | 技术报告正文，**基于赛事官方 Word 模板**（A4 21×29.7 cm、页边距左右 3.17 cm / 上下 2.54 cm、正文 12 pt 宋体、西文 Times New Roman、页眉赛事标识与页码沿用模板） |
| `design_report.pdf` | 同一份文档的 PDF 导出（WPS COM 导出，19 页） |
| `design_report.md` | 纯文本版正文，便于 diff 与检索；数字与 docx 一致 |
| `build_report_docx.py` | 生成 docx：以官方模板为母本，清空正文后按模板章节骨架重写，图文交替排布 |
| `build_report_pdf.py` | 用 WPS 的 COM 接口把 docx 导出成 PDF（本机没有 LibreOffice / pandoc） |
| `figures/` | 报告用图，见下表 |

## 正文结构（与模板一致）

作品名称 → 摘要（≤800 字）→ 第一部分 作品概述（功能与特性 / 应用领域 / 主要技术特点 / 主要性能指标 / 主要创新点 / 设计流程）→
第二部分 系统组成及功能说明（整体介绍 / 硬件系统 / 软件系统）→ 第三部分 完成情况及性能参数（整体介绍 / 工程成果 / 特性成果）→
第四部分 总结（可扩展之处 / 心得体会）→ 第五部分 参考文献（12 篇）。

## 图表清单与来源

| 编号 | 内容 | 来源文件 |
| --- | --- | --- |
| 图 1-1 | 作品实物（开发板 + 外接解码板 + 语音模块） | `figures/board_overview.jpg` |
| 图 1-2 | 三种降噪方案单帧成本 vs 10 ms 预算 | `figures/fig7_cost.png`（由 `材料库/06-交付文档/tools/make_figs.py` 用 PIL 绘制） |
| 图 2-1 | 系统整体框图 | `figures/fig1_system.png` |
| 图 2-2 | I²S 接线与双 DAC 并联 | `figures/fig2_wiring.png` |
| 图 2-3 | 软件分层与数据流 | `figures/fig3_sw.png` |
| 图 2-4 | 控制台调试模式 | `figures/fig4_ui_debug.png` |
| 图 2-5 | 控制台演示模式 | `figures/fig5_ui_demo.png` |
| 图 3-1 | 外接 ES9038Q2M 解码板与飞线 | `figures/dac_module.jpg` |
| 图 3-2 | 语音播报通道搭接（ESP32-S3 + 功放模块） | `figures/photo_module.jpg` |
| 图 3-3 | 有信号时的调试台 | `figures/fig6_ui_live.png` |
| 图 3-4 | 智能助手语音答复 | `figures/ui_ai_voice_reply.png` |

表 1-1 主要性能指标；表 2-1 系统模块与实现位置；表 2-2 主要器件清单；表 2-3 关键信号与引脚；
表 2-4 PL/PS/PC 功能划分；表 3-1 功能与性能实测对照；表 3-2 资源占用与时序结果。共 7 张表。

## 重新生成

```bat
rem 1) 生成 docx（官方模板必须在本机，路径见脚本里的 TPL 常量）
python build_report_docx.py

rem 2) 导出 PDF（需要本机装 WPS；脚本会先关掉占用中的旧文档再导出）
python build_report_pdf.py
```

两个脚本都可以重复跑：`build_report_docx.py` 若发现 `design_report.docx` 被 WPS/Word 占用，会自动写到
`design_report_v3.docx` 并在控制台提示；`build_report_pdf.py` 优先导出刚生成的那一份。

## 需要注意的三点

1. **数字以 `../metrics/metrics.csv` 为准**。报告里的帧率、丢包、资源、时序、功耗都取自实测记录，
   若后续测得新值，先改 metrics，再同步报告与 README，避免三处不一致。
2. **未测项如实标注**。端到端延迟、PL 谱减的 SNR 增益目前没有可信测法，报告与 metrics 都写"未测"，
   没有用估算值填充。
3. **斜 45° 整机照待补**。模板第三部分要求"正面、斜 45°"两张全局照，目前只有正面与局部特写，
   正文 3.1 已如实说明。补拍后替换 `figures/` 里的对应文件并重跑上面两条命令即可。
