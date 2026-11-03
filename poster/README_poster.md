# 海报（poster/）

会议海报，中英文各一版。**版式直接沿用组委会下发的 `AMD_poster_template.pptx`**：
页面 33.87×19.05 cm（A4 横向）、AMD 标识、黑色标题带、竖排装饰字、页脚页码全部保留，
面板位置与模板一致，只替换了内容文字与图片。

## 交付物

| 文件 | 说明 |
|---|---|
| `poster_zh.pptx` | 中文版，可直接改字后投稿 |
| `poster_zh.pdf` | 中文版 PDF（WPS 导出，用于先看效果） |
| `poster_en.pptx` | 英文版 |
| `poster_en.pdf` | 英文版 PDF |
| `../report/figures/fig_poster_arch.png` | 中文版系统框图（专为海报重画，字号比报告版大） |
| `../report/figures/fig_poster_arch_en.png` | 英文版系统框图 |
| `../report/figures/strip_scene.png` | 左列三张实物图拼成的横幅（统一圆角卡片） |
| `../report/figures/thumb_board.png` | 右上角"作品实物"缩略图 |
| `../report/figures/logo_badge_dark.png` | 校徽（深色版，1.62 cm 见方） |
| `../report/figures/logo_name_dark.png` | 校名（深色版，中文 7.05 cm / 英文 6.70 cm 宽） |
| `../report/figures/subject/` | 实物图的中间产物：`board_card.png`、`dac_card.png`（圆角卡片）、`module_cut.png`（抠好的 ESP32） |

## 生成方式

```bash
python tools/make_poster_figs.py        # 重画中/英系统框图
python tools/prep_poster_photos.py      # 实物照片：紧裁 + 圆角 + 投影
python tools/make_poster_assets.py      # 校徽/校名深色版 + 左列横幅 + 右上缩略图
python tools/build_poster.py --pdf      # 生成本目录下 4 个文件
```

`build_poster.py` 里的内容全部集中在文件开头的 `ZH` / `EN` 两个字典（标题、作者、正文、
两个圆角框、右栏两段、两处图注），改文案只动字典即可，不要改模板本身。

目标文件被 PowerPoint / WPS / 阅读器占用时，脚本不报错退出，而是把结果留在
`_stage_zh.pptx` / `_stage_xx.pdf`，关掉占用后手动改名即可（或重跑一次）。


## 还需要人工填的一处

**参赛队员**（已填）：中文版「参赛队员：温俊浩　林子源　姜远」；英文版「Team members: Junhao Wen, Ziyuan Lin, Yuan Jiang」（名在前、姓在后，与模板示例 "Hao Zhang" 一致）。
位置在标题右侧，字体 Times New Roman 12 pt 加粗。

校徽与校名已经放上（右上角，整枚 logo，深色版）。原始素材是白色透明底，
直接贴到白底上会看不见，`tools/make_poster_assets.py` 把字形重着色成 #1F2937、
保留原始 alpha、放大 3 倍，边缘不变毛。
**设计报告 docx 仍然不写校名**——那是赛事技术文档模板的要求，两份文件口径不同是故意的。

## 口径

- 所有数字与 `metrics/metrics.csv`、`report/design_report.docx` 一致：
  100.0 帧/秒、1.549 Mbit/s、丢包 0、欠载 2、−31.2 dBFS、LUT 19 656（36.95%）、
  FF 48 299（45.39%）、BRAM 8、DSP 0、WNS +0.357 ns、WHS +0.024 ns、功耗 2.189 W。
- 海报上出现了两条"对自己不利"的事实，是刻意的：布局密度 99.40%、RNNoise 20.07 ms 越线。
  它们正是"下一步把谱减搬进 PL、把 FIFO 存储阵列改成块 RAM"的理由，评审看的是判断力。
- 不写"国内领先""性能优异"这类无证据的形容词。

## 排版注意

- 中文版正文用微软雅黑（模板的 Aharoni/Calibri 没有中文字形），英文版保留模板原字体。
- 左列正文最长到 17.3 cm，右栏正文到 17.0 cm，**再加字会压到页脚**；要加内容请先删旧内容。
- 修改后务必 `--pdf` 重新导出并目视检查一页，WPS 对中文断行与字号的处理和 PowerPoint 略有差异。
