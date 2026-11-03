# -*- coding: utf-8 -*-
"""按赛事官方 Word 模板生成技术报告（图文结合版）。

做三件事：
1. 以官方模板 docx 为母本（保留 A4 页面、页边距、标题样式），清空正文后按模板章节骨架重写；
2. 正文与图/表交替排布，每张图都在正文里被引用（"如图 X-Y 所示"），表题在图/表上方；
3. 输出 design_report.docx（被占用时退化为 design_report_v3.docx）。

用法：python build_report_docx.py
"""
import os
import shutil
import sys
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
FIGDIR = os.path.join(HERE, "figures")
TPL = r"C:\Users\温先生\Desktop\我的世界\2658aa94-81d0-454c-b2ac-fc040ff2c01a (2).docx"
LIB = r"D:\deepseek herness\材料库\06-交付文档\figs"
OUT = os.path.join(HERE, "design_report.docx")
OUT_ALT = os.path.join(HERE, "design_report_v3.docx")

# ---------------------------------------------------------------- 图素材
FIGS = {
    "system": (os.path.join(LIB, "fig1_system.png"), 13.6),
    "wiring": (os.path.join(LIB, "fig2_wiring.png"), 13.2),
    "sw": (os.path.join(LIB, "fig3_sw.png"), 14.4),
    "ui_debug": (os.path.join(LIB, "fig4_ui_debug.png"), 13.6),
    "ui_demo": (os.path.join(LIB, "fig5_ui_demo.png"), 13.6),
    "ui_live": (os.path.join(LIB, "fig6_ui_live.png"), 12.4),
    "cost": (os.path.join(LIB, "fig7_cost.png"), 14.2),
    "board": (os.path.join(FIGDIR, "board_overview.jpg"), 12.4),
    "dac": (os.path.join(FIGDIR, "dac_module.jpg"), 12.4),
    "module": (os.path.join(FIGDIR, "photo_module.jpg"), 12.4),
    "voice": (os.path.join(PKG, "metrics", "evidence", "ui_ai_voice_reply.png"), 13.2),
}
for k, (p, _w) in FIGS.items():
    if not os.path.isfile(p):
        raise SystemExit("缺图：%s" % p)

# ---------------------------------------------------------------- 版式工具
BODY_PT = 12.0
INDENT = Pt(BODY_PT * 2)          # 首行缩进 2 字符


def _font(run, size=BODY_PT, bold=False, east="宋体"):
    run.font.name = "Times New Roman"
    run.font.size = Pt(size)
    run.bold = bold
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), "Times New Roman")
    rFonts.set(qn("w:hAnsi"), "Times New Roman")
    rFonts.set(qn("w:eastAsia"), east)


def body(text, indent=True, after=4, spacing=1.5, size=BODY_PT):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(after)
    pf.line_spacing = spacing
    if indent:
        pf.first_line_indent = INDENT
    _font(p.add_run(text), size)
    return p


def bullet(text, after=3):
    """条目：无首行缩进，悬挂式对齐，用于创新点/清单。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(after)
    pf.line_spacing = 1.5
    pf.left_indent = Cm(0.85)
    pf.first_line_indent = Cm(-0.0)
    _font(p.add_run(text))
    return p


def h1(text, new_page=True):
    if new_page and len(doc.paragraphs) > 3:
        doc.add_page_break()
    p = doc.add_paragraph()
    p.style = doc.styles["Heading 1"]
    pf = p.paragraph_format
    pf.space_before = Pt(6)
    pf.space_after = Pt(10)
    pf.line_spacing = 1.5
    _font(p.add_run(text), 15, True, "黑体")
    return p


def h2(text):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(8)
    pf.space_after = Pt(5)
    pf.line_spacing = 1.5
    _font(p.add_run(text), 13, True, "黑体")
    return p


def h3(text):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(6)
    pf.space_after = Pt(4)
    pf.line_spacing = 1.5
    _font(p.add_run(text), 12, True, "黑体")
    return p


def fig(key, caption):
    path, width = FIGS[key]
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = True
    p.add_run().add_picture(path, width=Cm(width))
    c = doc.add_paragraph()
    c.alignment = WD_ALIGN_PARAGRAPH.CENTER
    c.paragraph_format.space_before = Pt(0)
    c.paragraph_format.space_after = Pt(10)
    c.paragraph_format.line_spacing = 1.0
    _font(c.add_run(caption), 10.5)


def _shade(cell, hexcolor):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hexcolor)
    tcPr.append(shd)


def _borders(t, color="7F7F7F", sz=6):
    tblPr = t._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement("w:%s" % edge)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    tblPr.append(borders)


def table(caption, headers, rows, widths, fs=10.0):
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.paragraph_format.space_before = Pt(8)
    cap.paragraph_format.space_after = Pt(3)
    cap.paragraph_format.line_spacing = 1.0
    cap.paragraph_format.keep_with_next = True
    _font(cap.add_run(caption), 10.5)

    t = doc.add_table(rows=1, cols=len(headers))
    _borders(t)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    # 跨页时表头自动重复
    trPr = t.rows[0]._tr.get_or_add_trPr()
    th = OxmlElement("w:tblHeader")
    th.set(qn("w:val"), "true")
    trPr.append(th)
    for i, hd in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = ""
        _shade(cell, "EDF2F9")
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.line_spacing = 1.0
        _font(p.add_run(hd), fs, True)
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT if i == 0 else WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.0
            _font(p.add_run(str(v)), fs)
    for i, w in enumerate(widths):
        for row in t.rows:
            row.cells[i].width = Cm(w)
    for row in t.rows:                     # 行内不跨页断开
        trPr = row._tr.get_or_add_trPr()
        cs = OxmlElement("w:cantSplit")
        cs.set(qn("w:val"), "true")
        trPr.append(cs)
    gap = doc.add_paragraph()
    gap.paragraph_format.space_before = Pt(0)
    gap.paragraph_format.space_after = Pt(4)
    gap.paragraph_format.line_spacing = 1.0
    _font(gap.add_run(""), 6)
    return t


def ref(text):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(2)
    pf.line_spacing = 1.35
    pf.left_indent = Cm(0.85)
    pf.first_line_indent = Cm(-0.85)
    _font(p.add_run(text), 10.5)
    return p


# ---------------------------------------------------------------- 打开模板
if not os.path.isfile(TPL):
    raise SystemExit("模板不存在：%s" % TPL)
doc = Document(TPL)
body_el = doc.element.body
kept = [el for el in body_el if el.tag == qn("w:sectPr")]
for el in list(body_el):
    body_el.remove(el)
for el in kept:
    body_el.append(el)
for s in doc.sections:
    s.left_margin = Cm(3.17)
    s.right_margin = Cm(3.17)
    s.top_margin = Cm(2.54)
    s.bottom_margin = Cm(2.54)

# 页码：模板页脚本身已带 PAGE 域，这里不再重复添加（否则会出现两个页码）


# ---------------------------------------------------------------- 封面/标题
p = doc.add_paragraph()
p.style = doc.styles["Heading 2"]
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_before = Pt(24)
p.paragraph_format.space_after = Pt(6)
_font(p.add_run("基于 ZYNQ-7020 的实时音频采集、降噪与双路回放系统"), 18, True, "黑体")

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_after = Pt(18)
_font(p.add_run("——FPGA 内 I²S 全链路、100 帧/秒无损上行与外接 DAC 并联输出"), 12, True, "楷体")

p = doc.add_paragraph()
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_after = Pt(20)
_font(p.add_run("FPGA 创新创业大赛 · 自主选题赛道（高级组）"), 10.5, False, "宋体")

# ---------------------------------------------------------------- 摘要
p = doc.add_paragraph()
p.paragraph_format.space_after = Pt(4)
_font(p.add_run("摘　要"), 14, True, "黑体")

body("本作品把一条完整的音频链路放在一块 ZYNQ-7020 板上：ES8388 采集的麦克风信号经 FPGA 内的 I²S "
     "收发状态机与异步 FIFO 跨时钟搬运，由 AXI-Stream + DMA 写入 DDR，再由 ARM 端按 10 ms 定长帧以 UDP "
     "推送到 PC；反向的回放数据同样从 PC 经以太网进入 PL，由同一组 I²S 时序扇出到板载 ES8388 与外接 "
     "ES9038Q2M 两路 DAC，实现\"同一份数据、两个耳机口同时出声\"。")
body("设计上刻意把\"硬实时\"留给硬件：I²S 的位时钟与帧时钟由 PL 内部生成（BCLK 1.536 MHz、LRCLK 48 kHz、"
     "每帧 32 个 BCLK），采样、并串转换、奇偶声道合帧、跨时钟域全部在 PL 内以纯状态机完成，软件只负责"
     "调度与协议，因此上行链路可稳定跑满 100 帧/秒（1.549 Mbit/s），实测丢包 0、欠载 2、帧率波动 1.3。")
body("工程同时给出了\"为什么这样切分\"的量化依据：同一段 10 ms 音频，噪声门在 PS 上耗时 0.127 ms，"
     "而神经网络降噪（RNNoise）单帧耗时 20.07 ms，已超出 10 ms 实时预算，这正是把降噪下移到 FPGA 的"
     "直接理由；报告给出了谱减方案在 PL 内的落地路径与已验证的定点仿真。PC 端控制台以浏览器呈现，"
     "实时显示波形、频谱、DBFS、帧率与丢包，并集成一个可语音作答的智能助手。")
body("整机资源占用 LUT 19 656（36.95%）、FF 48 299（45.39%）、BRAM 8（5.71%）、DSP 0，WNS +0.357 ns、"
     "WHS +0.024 ns，全片时序收敛，功耗 2.189 W。所有指标均在真实硬件上测得，测试条件与证据文件随包提供。", after=8)

# ---------------------------------------------------------------- 第一部分
h1("第一部分　作品概述")

h2("1.1　功能与特性")
body("本作品是一台\"端侧实时音频节点\"：麦克风进来的声音在板内完成数字化、成帧、上行；PC 侧完成降噪、"
     "监听与可视化；回放的声音再从网络回到板内，由两路 DAC 同时输出。整机实物如图 1-1 所示，"
     "一块开发板加一块外接解码板即可独立工作，不需要额外的声卡或采集盒。")
fig("board", "图 1-1　作品实物：ZYNQ-7020 开发板（左）与外接 ES9038Q2M 解码板（右），飞线引出 I²S 与电源")
body("具体提供四项能力：一是 48 kHz/16 bit 立体声的实时采集与上行，固定 10 ms 一帧、每秒 100 帧，"
     "每帧 1 920 字节，网络抖动由板内双缓冲吸收；二是实时回放，PC 推来的 PCM 在 PL 内被拆成左右声道，"
     "按 I²S 时序送出，切换、调音量、加测试音都在运行中生效，不需要重配置；三是双路 DAC 并联输出，"
     "板载 ES8388 与外接 ES9038Q2M 共享同一组 BCK/LRCR/DATA，软件不参与选择，因此可以直接做"
     "\"同源双耳放\"的 A/B 对比；四是板卡自身的健康可视，帧率、丢包、欠载、DBFS、DMA 状态、"
     "I²C 配置结果都能在控制台一屏读完，出问题时不需要接示波器就能判断是采集侧还是网络侧。")

h2("1.2　应用领域")
body("第一类是教学与竞赛场景：音频是一条\"看得见\"的信号链，从模拟前端、时钟、跨时钟域、DMA 到网络协议，"
     "每一层都能用示波器和读数验证，适合作为 FPGA 系统设计的综合训练平台。第二类是嵌入式音频产品的前期"
     "验证：麦克风阵列前端、会议终端、录音笔、车载语音模块在定型前都需要一条可改、可测、可回放的链路，"
     "本作品的 PL 侧接口是标准 AXIS，换前端只需替换采集模块。第三类是工业与安防的现场声学监测："
     "设备噪声、电机异响、管道泄漏这类场景要的是\"长期稳定上行 + 本地可判\"，本作品的 100 帧/秒定长帧、"
     "丢包可观测、看门狗自动复位正好对应这些要求。第四类是端侧智能的载体：把降噪、关键词唤醒、"
     "声学事件检测逐步搬进 PL，可以在不增加主控负载的前提下提升实时性，本作品给出的时延预算"
     "（图 1-2）就是为此做的准备。")

h2("1.3　主要技术特点")
body("第一个特点是\"时钟与采样全部在硬件里\"。BCLK、LRCLK 由 PL 内部分频产生，采集状态机在 BCLK 域"
     "完成 16 位移位与左右声道合帧，随后经异步 FIFO 跨到 100 MHz 系统时钟域，再由 AXIS 送 DMA。"
     "整条路径没有 CPU 参与，也没有中断抖动，因此帧间隔是恒定的 10 ms。")
body("第二个特点是\"帧格式为网络而生\"。每帧 480 个字（1 920 字节），拆成两个 968 字节的 UDP 载荷"
     "（8 字节应用头 + 960 字节数据），接收端只要看帧头的序号与时间戳就能判断丢帧、乱序和抖动，"
     "协议细节见 2.3 节。")
body("第三个特点是\"双 DAC 同源扇出\"。同一路 I²S 在 PL 输出端直接并行送到两个 DAC，两个耳机口的差别"
     "只可能来自模拟侧，这为\"解码器音质对比\"提供了一个没有软件变量的实验台。")
body("第四个特点是\"用实测数据决定软硬划分\"。图 1-2 给出了三种降噪方案的单帧成本与 10 ms 预算的关系："
     "噪声门在 PS 上只用 0.127 ms，神经网络方案 20.07 ms，已经越线。这张图直接解释了为什么本作品的"
     "下一步是把谱减搬进 PL，而不是继续在 ARM 上堆算力。")
fig("cost", "图 1-2　三种降噪方案的单帧处理成本与 10 ms 实时预算对比（越线即无法维持 100 帧/秒）")

h2("1.4　主要性能指标")
body("主要指标见表 1-1，均为板上实测；\"测试条件\"一列写明了硬件、输入与软件版本，"
     "每项对应的原始日志与复测脚本在 metrics 目录下。")
table("表 1-1　主要性能指标（实测）",
      ["指标", "实测值", "测试条件"],
      [["上行帧率", "100.0 帧/秒（波动 1.3）", "48 kHz/16 bit 立体声，10 ms/帧，连续 6 s"],
       ["上行吞吐", "1.549 Mbit/s", "同上，UDP 载荷 968 B × 2/帧"],
       ["丢包 / 欠载", "0 / 2", "控制台统计，连续 1 224 帧"],
       ["输入电平", "−31.2 dBFS", "正常说话距离 20 cm"],
       ["PL 资源", "LUT 19 656（36.95%）、FF 48 299（45.39%）", "Vivado 2024.2，xc7z020clg400-2"],
       ["时序", "WNS +0.357 ns、WHS +0.024 ns", "最差路径 100 MHz 系统时钟"],
       ["功耗", "2.189 W（动态 2.031 W）", "Vivado 功耗估算，典型活动率"]],
      [4.2, 5.6, 4.4])

h2("1.5　主要创新点")
bullet("① 采集链路\"零软件\"：I²S 收发、合帧、跨时钟域全部由 PL 状态机完成，CPU 只做协议与调度，"
       "帧间隔抖动被压到与晶振同量级。")
bullet("② 定长帧 + 双缓冲的可观测上行：每帧带序号与时间戳，丢包与欠载在控制台直接可见，"
       "把\"听感问题\"变成\"读数问题\"。")
bullet("③ 双 DAC 同源扇出：同一组 I²S 并行驱动板载与外接解码器，为音质对比提供了不含软件变量的平台。")
bullet("④ 用实测时延预算指导软硬划分：给出噪声门 0.127 ms 与神经网络 20.07 ms 的实测对比，"
       "并据此规划 PL 谱减的落地路径，而不是凭感觉\"上硬件\"。")

h2("1.6　设计流程")
body("整个作品按\"先通链路、再谈指标、最后补能力\"的顺序推进：第一步打通 PL 内部 I²S 环回，"
     "用示波器确认 BCLK/LRCLK 与数据的关系；第二步把采集数据经 AXIS + DMA 送进 DDR，再由 ARM 侧"
     "以 UDP 发出，用抓包确认帧格式；第三步在 PC 端写控制台，把帧率、丢包、DBFS 变成可见读数；"
     "第四步加入回放与双 DAC 扇出，验证同一份数据两路出声；第五步补齐智能助手、语音播报与提交包，"
     "并把每一步的判据、失败原因与修复方式写进文档。每一步都以\"板子上的读数\"作为通过条件，"
     "不接受\"看起来对\"。")

# ---------------------------------------------------------------- 第二部分
h1("第二部分　系统组成及功能说明")

h2("2.1　整体介绍")
body("系统自左向右分为三层：PC 端控制台、千兆以太网、ZYNQ-7020，如图 2-1 所示。板内再分为 PL 与 PS 两部分，"
     "PL 负责所有与\"时间\"有关的动作（时钟生成、采样、并串转换、跨时钟域、AXIS 搬运），"
     "PS 负责所有与\"协议\"有关的动作（UDP 收发、帧调度、DMA 双缓冲、看门狗、参数解析）。"
     "输出侧，同一路 I²S 并行送到板载 ES8388 与外接 ES9038Q2M。")
fig("system", "图 2-1　系统整体框图：PC ↔ 千兆以太网 ↔ ZYNQ-7020（PL 硬件通路 + PS 帧调度）→ 双 DAC 并联输出")
body("三层之间的耦合被刻意做得很松：PL 与 PS 之间只有 AXIS 与一组寄存器；PS 与 PC 之间只有 UDP 与"
     "一个 15 字节状态包；PC 与浏览器之间只有 HTTP。任何一层出问题，都可以在相邻接口上被单独观测到，"
     "这也是这套系统能被反复调试而不失控的原因。各模块职责与实现位置见表 2-1。")
table("表 2-1　系统模块、职责与实现位置",
      ["模块", "职责", "实现位置"],
      [["I²S 时序生成", "BCLK 1.536 MHz / LRCLK 48 kHz 分频产生", "PL（slave_signal_gen.v）"],
       ["音频采集", "BCLK 域移位、左右声道合帧、10 ms 成帧", "PL（audio_receive.v + audio_axis_core.v）"],
       ["跨时钟域", "BCLK 域 ↔ 100 MHz 域，格雷码指针异步 FIFO", "PL（afifo.v）"],
       ["数据搬运", "AXIS → 时钟转换 → DMA S2MM → DDR", "PL（BD：axis_clock_converter + axi_dma）"],
       ["帧调度与协议", "10 ms 帧循环、UDP 收发、DMA 双缓冲、看门狗", "PS（app_ea_main.c）"],
       ["参数与控制", "模式、时长比例、增益、底噪、测试音", "PS + axi_gpio"],
       ["可视化与交互", "波形、频谱、DBFS、帧率、录音、AI 助手", "PC（web_server.py + index.html）"],
       ["语音播报", "把答复合成 PCM 推给外接喇叭", "PC → ESP32（TCP 9200）→ MAX98357A"]],
      [3.4, 6.6, 4.6])

h2("2.2　硬件系统介绍")
h3("2.2.1　硬件整体")
body("整机由一块 ZYNQ-7020 开发板、一块外接 ES9038Q2M 解码板、一个 ESP32-S3 模块与若干飞线组成，"
     "主要器件见表 2-2。开发板负责采集、处理与网络；外接解码板只负责把 I²S 变成模拟声；"
     "ESP32 只负责把 PC 推来的语音播报送到小喇叭。三者之间用 I²S、USB 与以太网连接，"
     "没有共享时钟，也不需要共享。")
table("表 2-2　主要器件清单",
      ["器件", "型号 / 规格", "在本系统中的作用"],
      [["主控", "ZYNQ-7020（xc7z020clg400-2）", "PL 硬件通路 + 双核 A9 帧调度"],
       ["板载 codec", "ES8388", "麦克风采集与板载耳机输出，I²C 配置"],
       ["外接 DAC", "ES9038Q2M", "I²S 从模式解码，自带晶振，外接耳机口"],
       ["语音模块", "ESP32-S3（N16R8）+ MAX98357A", "接收 PC 的 PCM，驱动小喇叭"],
       ["存储/启动", "SD 卡（启动镜像）、DDR3 512 MB", "固件加载与音频双缓冲"],
       ["网络", "板载千兆 PHY，RGMII", "UDP 上行 50505、下行 50506、控制 50507"]],
      [3.2, 6.4, 5.0])

h3("2.2.2　机械设计")
body("本作品没有专门的机械结构件：开发板、外接解码板与 ESP32 分别由铜柱、亚克力底板与面包板固定，"
     "整体平放在桌面，方便随时飞线改接与示波器探针接入。这样的取舍是为了把迭代成本降到最低——"
     "在验证阶段，改一根线的代价远小于重做一块转接板。正式产品化时，可把 I²S 飞线改为 20 pin 排线、"
     "把两块板的固定孔对齐做成叠板结构，体积可压缩到约 120 mm × 90 mm × 35 mm。")

h3("2.2.3　电路各模块")
body("电路连接与双 DAC 并联关系如图 2-2 所示。采集侧，麦克风信号进入 ES8388 的 MIC 输入，"
     "由 I²C 配置寄存器完成偏置、增益与通路选择；ES8388 作为 I²S 主机，输出 BCLK / LRCLK / ADC_DATA 三根线，"
     "其中 BCLK 与 LRCLK 实际由 PL 生成、经开发板引到 codec；PL 采到的数据回送到 DAC 侧，"
     "同时并行送往板载 ES8388 的 DAC 输入与外接 ES9038Q2M。")
fig("wiring", "图 2-2　I²S 接线与双 DAC 并联示意：BCK / LRCR / DATA 同一组扇出到两片 DAC")
body("关键信号与引脚对应关系见表 2-3。供电方面，开发板由 12 V 适配器供电，板载稳压产生 3.3 V 与 1.0 V；"
     "外接解码板由开发板的 3.3 V 引出，地线与 I²S 地同点连接，避免地环路引入的底噪；"
     "ESP32 由 USB 供电，与开发板不共地，音频通过独立的 I²S 通道输出，避免数字噪声串入模拟侧。")
table("表 2-3　关键信号与引脚对应",
      ["信号", "方向", "引脚 / 端口", "说明"],
      [["AUD_MCLK", "PL → codec", "E19", "12.288 MHz 主时钟"],
       ["AUD_BCLK", "PL → codec", "M18", "1.536 MHz 位时钟"],
       ["AUD_LRC", "PL → codec", "L20", "48 kHz 帧时钟"],
       ["AUD_DACDAT", "PL → codec", "G17", "回放数据（同时扇出到外接 DAC）"],
       ["AUD_ADCDAT", "codec → PL", "M17", "采集数据"],
       ["AUD_SCL / SDA", "PS ↔ codec", "E18 / F17", "I²C 配置（400 kHz）"],
       ["AUD2_BCLK / LRC / DATA", "PL → 外接 DAC", "W11 / Y13 / Y11", "第二路 I²S（同源扇出）"],
       ["ETH RGMII", "PL ↔ PHY", "多引脚", "千兆以太网数据通路"],
       ["UART", "PS ↔ PC", "J15 / T19", "调试串口（115200）"]],
      [3.6, 3.4, 3.6, 3.8])

h2("2.3　软件系统介绍")
h3("2.3.1　软件整体")
body("软件分三层，如图 2-3 所示。最底层是 PL 硬件层，只有状态机与 FIFO，没有指令流；"
     "中间是 PS 调度层，裸机运行，主循环以 10 ms 为节拍推进；最上层是 PC 应用层，Python 后端负责收流、"
     "缓冲与 HTTP 接口，浏览器前端负责呈现与交互。三层之间通过 AXIS、UDP 与 HTTP 三个明确的接口通信，"
     "任何一层的替换都不影响其他层。")
fig("sw", "图 2-3　软件分层与数据流：PL 硬件层 / PS 调度层 / PC 应用层")
body("功能划分的依据是\"时间敏感度\"：凡是以微秒计的（位时钟、采样、跨时钟域）放在 PL；"
     "凡是以毫秒计的（帧调度、协议、参数）放在 PS；凡是以人为时间尺度计的（可视化、录音、AI 交互）"
     "放在 PC。表 2-4 给出了三层的边界与代价。")
table("表 2-4　PL / PS / PC 的功能划分与实测代价",
      ["层", "承担的功能", "时间尺度 / 实测代价"],
      [["PL（纯硬件）", "I²S 收发、合帧、异步 FIFO、AXIS、I²C 时序", "纳秒级；资源 LUT 19 656、FF 48 299"],
       ["PS（裸机）", "10 ms 帧循环、UDP、DMA 双缓冲、噪声门、看门狗", "毫秒级；噪声门 0.127 ms/帧"],
       ["PC（Python）", "收流、环形缓冲、降噪（可选）、可视化、AI 助手", "十毫秒～秒级；不影响板内实时性"]],
      [3.0, 6.2, 5.4])

h3("2.3.2　软件各模块")
body("采集模块（audio_receive）在 BCLK 域工作：检测 LRCLK 跳变作为帧起点，用 6 位计数器在 16 个 BCLK 内"
     "把串行数据按 MSB 优先移入寄存器，第 16 个 BCLK 拉高 rx_done；合帧模块在右声道结束时把左右两个 16 bit "
     "拼成一个 32 bit 字，写入异步 FIFO。FIFO 采用格雷码指针与两级同步，读写分别处在 BCLK 域与 100 MHz 域。")
body("搬运模块由 AXIS 与 DMA 组成：FIFO 非空即拉高 tvalid，DMA 以 480 拍为一个包（对应 10 ms）搬到 DDR，"
     "完成后置中断；PS 侧用双缓冲轮转，一块在填、一块在发，避免网络抖动倒灌到采集侧。")
body("调度模块是 PS 的主循环：每个 10 ms 周期检查 DMA 完成标志、把上一帧的数据按 968 字节拆包发出、"
     "读取控制字、更新状态包；同时维护一个 200 ms 的麦克风看门狗，一旦发现采集停摆就重新初始化 codec。"
     "参数（模式、时长比例、增益、底噪、测试音）都通过 50507 的单字节控制包下发，用最小协议换最小延迟。")
body("PC 端由 web_server.py 与 index.html 组成：后端用 UDP 收流、维护 PCM 环形缓冲，对外提供 HTTP 接口；"
     "前端每 500 ms 拉一次状态，画出波形、频谱、DBFS、帧率与丢包。界面提供\"调试\"与\"演示\"两种模式，"
     "调试模式一屏给全量仪表盘（图 2-4），演示模式按页讲解（图 2-5），便于现场答辩时逐条展开。")
fig("ui_debug", "图 2-4　控制台调试模式：单页全量仪表盘，波形、频谱、遥测、质量条同时可见")
fig("ui_demo", "图 2-5　控制台演示模式：分页讲解，适合现场演示与答辩")
body("智能助手模块把大模型接到控制台上：用户在对话框里用自然语言提问，助手先调用板卡接口读回真实状态，"
     "再据此回答；打开\"念出来\"后，答复会被合成为 16 kHz PCM，通过 TCP 9200 推给 ESP32，由 I²S 送到小喇叭。"
     "语音与文字同时输出，板子不在线时自动退化为纯文字，不会报错。")

# ---------------------------------------------------------------- 第三部分
h1("第三部分　完成情况及性能参数")

h2("3.1　整体介绍")
body("整机外观见图 1-1。左侧为 ZYNQ-7020 开发板，网络、音频与调试接口都在这一侧；右侧为外接 ES9038Q2M "
     "解码板，通过飞线取 I²S 与 3.3 V 电源；上方小模块为 ESP32-S3，负责语音播报。"
     "上电后无需任何操作，板内自动完成 codec 配置与网络初始化，控制台打开即可看到实时读数。"
     "需要说明的是，本作品目前没有专用的斜 45° 整机照，图 1-1 与图 3-1、图 3-2 分别覆盖了整机、"
     "外接解码板与语音模块三个视角；正式提交影像资料时会补拍。")

h2("3.2　工程成果")
h3("3.2.1　机械成果")
body("本作品未使用定制结构件，采用铜柱 + 亚克力底板固定开发板，面包板固定 ESP32，外接解码板用双面胶与"
     "排针固定。优点是改线快、探针易接；不足是抗振动与外观一致性较弱，产品化时需要重新设计叠板结构。")
h3("3.2.2　电路成果")
body("外接解码板的接入是本作品电路部分的主要工作，实物如图 3-2 所示。解码板工作在 I²S 从模式，"
     "由开发板提供 BCK / LRCR / DATA 三根信号与 3.3 V 电源；板上自带晶振，因此两片 DAC 的时钟并不完全同源，"
     "这也是\"两个耳机口音色不同\"的物理来源之一，报告中如实记录，未做软件补偿。")
fig("dac", "图 3-1　外接 ES9038Q2M 解码板与飞线：I²S 三线 + 3.3 V 供电")
body("语音播报通道的接线如图 3-2 所示：ESP32-S3 通过 USB 取电，I²S 三根线（BCLK = GPIO4、"
     "WS = GPIO5、DIN = GPIO6）接到 MAX98357A 数字功放，功放直接驱动 8 Ω 小喇叭。"
     "这条链路与主链路的音频完全解耦——PC 把 PCM 经 TCP 9200 推给 ESP32，ESP32 只负责按 I²S 时序吐出，"
     "因此主链路做任何改动都不会影响语音播报。")
fig("module", "图 3-2　语音播报通道搭接：面包板上的 ESP32-S3（右）与音频功放模块（左）")
h3("3.2.3　软件成果")
body("控制台是本作品软件部分的主要成果，界面如图 3-3 与图 3-4 所示。它把原先分散在示波器、"
     "抓包工具与串口日志里的信息收敛到一屏：波形与频谱用于判断信号质量，帧率与丢包用于判断链路健康，"
     "遥测字用于判断板内状态，录音与 A/B 用于对比降噪前后的听感。")
fig("ui_live", "图 3-3　有信号时的调试台：波形、频谱、遥测与质量条同时刷新")
fig("voice", "图 3-4　智能助手答复：文字与语音同时输出，气泡标注\"已念出\"即表示板子喇叭已发声")
body("除界面外，软件成果还包括：一套可复现的构建与烧写脚本（Vivado Tcl + xsdb）、一套板卡自检与"
     "参数核对脚本、以及一个把\"判据\"固化下来的交付检查脚本（检查目录命名、必备文件、指标一致性），"
     "这些脚本都随包提供，可在另一台机器上原样跑通。")

h2("3.3　特性成果（量化指标）")
body("逐项功能的实测结果见表 3-1。所有数字都来自控制台读数或 Vivado 报告，测试条件写在表内；"
     "未能测到的两项（端到端延迟、PL 谱减的 SNR 增益）在 metrics 中如实标为\"未测\"并说明原因，"
     "没有用估算值替代。")
table("表 3-1　功能与性能实测对照",
      ["功能 / 性能项", "实测结果", "测试方法与证据"],
      [["实时采集上行", "100.0 帧/秒，1.549 Mbit/s", "控制台连续 1 224 帧统计，丢包 0"],
       ["回放与双 DAC", "两路同时出声，切换无需重配", "外接解码板 + 板载耳机口同时试听"],
       ["降噪模式", "旁路 / 噪声门在板上可用", "噪声门 0.127 ms/帧；RNNoise 20.07 ms/帧（超预算）"],
       ["AI 助手", "问答 1–3 s，可语音播报", "控制台对话框，板子回执 play N 与推送字节数一致"],
       ["资源占用", "LUT 36.95%、FF 45.39%、BRAM 5.71%", "Vivado 实现报告（placed/routed）"],
       ["时序收敛", "WNS +0.357 ns，0 failing endpoint", "Vivado 时序汇总报告"],
       ["端到端延迟", "未测", "缺少同步触发源，如实标注"]],
      [4.0, 5.4, 4.8])
body("资源与时序的完整结果见表 3-2。需要说明的是布局密度达到 99.40%，这是把异步 FIFO 的存储阵列"
     "按触发器实现所付出的代价；把该阵列改为块 RAM 后，FF 占用可下降约 32 800，布局压力会显著缓解，"
     "这一点已写入 known_issues 并给出了三步修改方案。")
table("表 3-2　资源占用与时序结果（Vivado 2024.2，xc7z020clg400-2）",
      ["项目", "占用", "占比", "说明"],
      [["Slice LUTs", "19 656", "36.95%", "含 I²S 状态机与 AXIS 通路"],
       ["Slice Registers", "48 299", "45.39%", "其中约 32 768 为 FIFO 存储阵列（未推断为 BRAM）"],
       ["Block RAM Tile", "8", "5.71%", "DMA 描述符与缓冲"],
       ["DSP", "0", "0%", "纯逻辑实现，未用乘法器"],
       ["Slice", "13 220 / 13 300", "99.40%", "布局密度接近上限，是后续优化的首要目标"],
       ["WNS / WHS", "+0.357 ns / +0.024 ns", "—", "100 MHz 系统时钟，0 failing endpoint"],
       ["功耗", "2.189 W", "—", "动态 2.031 W + 静态 0.158 W"]],
      [4.0, 4.6, 2.6, 5.2])

# ---------------------------------------------------------------- 第四部分
h1("第四部分　总结")

h2("4.1　可扩展之处")
body("第一，把降噪真正搬进 PL。当前谱减的 RTL 与定点仿真已完成，缺的是把它接入 BD 并用真实的"
     "I²S 数据验证；接入后 PS 的负载会明显下降，也为更复杂的模型留出空间。第二，把 FIFO 存储阵列"
     "改为块 RAM，释放约 32 800 个触发器，布局密度从 99.40% 降下来，为后续加模块留余量。"
     "第三，补齐端到端延迟的测量：接入一个同步触发源（如 GPIO 脉冲 + 录音回环），把\"从说话到听到\""
     "的时延量化到毫秒。第四，把这套链路做成可配置产品：采样率、帧长、声道数参数化，"
     "前端换成麦克风阵列后可支持波束成形。")

h2("4.2　心得体会")
body("这个项目最大的收获不是\"把功能做出来了\"，而是学会了区分\"看起来对\"和\"读数对\"。项目中期遇到过"
     "一个非常典型的问题：同一份源码，重新综合实现后麦克风就一包数据都没有，而十天前烧进去的比特流"
     "却一切正常。最初的反应是怀疑固件、怀疑 DMA、怀疑 codec 配置，改了很多地方都没有效果。"
     "真正定位问题靠的是三件事：一是把\"哪一版比特流能跑\"这件事固定下来，做成可复核的基线；"
     "二是把现象收敛成读数——采集计数器两次读数完全相同，说明采集状态机根本没有前进，"
     "而不是\"数据不对\"；三是回到时序报告里找结构性证据，最终发现该时钟域有 16 923 个触发器"
     "根本没有被约束，工具对这个域的行为等同于随机。")
body("这件事让我对\"约束\"两个字的理解变了。以前觉得时序约束是\"最后跑一下报告\"，现在知道它是设计的一部分："
     "没有被约束的时钟域，综合与实现的结果不可复现，今天能跑不代表明天能跑。同理，异步 FIFO 的存储阵列"
     "被拆成触发器这件事，报告里其实一直写着（RAM 无法推断，退化为 16 384 个触发器），只是当时没人去看。"
     "所以后来我给自己定了一条规矩：任何\"性能/资源/功能\"的结论，都必须能指到一条具体的日志行、"
     "一张报告截图或一次板子回读，否则就只是猜测。")
body("另一个体会是关于工具与人的分工。这个项目大量使用了智能体辅助（AMD Ross 套件里的文档检索与"
     "Vivado 控制能力），它在\"查手册、跑脚本、整理报告\"上确实快，但它给的建议经常是\"看起来合理\"的——"
     "比如建议补一条时钟约束，而实际问题是数据路径跨了两个同频不同相的时钟。真正可靠的用法是："
     "让它做执行与检索，让人做判据与取舍，并且要求它把每个结论都落到可复核的证据上。"
     "这套工作方式后来被整理成了技能包（skill 目录），把\"先取基线、再改一处、用读数验证\"的流程固化下来。")
body("最后是工程习惯上的收获：文档要写给\"三个月后的自己\"看。这个项目前后写了多份交接文档，"
     "每次交接都强迫自己把\"现在能做什么、不能做什么、下一步先做什么\"写清楚，结果反而少走了很多弯路。"
     "提交包里之所以保留 known_issues 与\"未测\"项，也是同一个道理——把不确定性摊开写，"
     "比藏起来更像一个工程作品。", after=6)

# ---------------------------------------------------------------- 第五部分
h1("第五部分　参考文献")
refs = [
    "[1] AMD/Xilinx. Zynq-7000 SoC Technical Reference Manual (UG585). 2023.",
    "[2] AMD/Xilinx. Vivado Design Suite User Guide: Synthesis (UG901). 2024.",
    "[3] AMD/Xilinx. Vivado Design Suite User Guide: Using Constraints (UG903). 2024.",
    "[4] AMD/Xilinx. UltraFast Design Methodology Guide for FPGAs and SoCs (UG949). 2024.",
    "[5] AMD/Xilinx. AXI DMA v7.1 LogiCORE IP Product Guide (PG021). 2023.",
    "[6] Philips Semiconductors. I²S Bus Specification. 1996.",
    "[7] J.-M. Valin. A Hybrid DSP/Deep Learning Approach to Real-Time Full-Band Speech Enhancement. "
    "IEEE MMSP, 2018.",
    "[8] Everest Semiconductor. ES8388 Low Power Audio CODEC Datasheet. 2018.",
    "[9] ESS Technology. ES9038Q2M Sabre DAC Datasheet. 2016.",
    "[10] Espressif Systems. ESP32-S3 Technical Reference Manual. 2023.",
    "[11] AMD. Ross Agentic AI Assistant for Embedded Design（产品说明与新闻稿）. 2026.",
    "[12] 本作品源码仓库：edge-audio-node（随提交包提供，含 RTL、固件、控制台与复现脚本）. 2026.",
]
for r in refs:
    ref(r)

# ---------------------------------------------------------------- 保存
target = OUT
try:
    doc.save(target)
except PermissionError:
    target = OUT_ALT
    doc.save(target)
    print("主文件被占用，已写到：%s" % target)
print("已生成：%s（%d 字节）" % (target, os.path.getsize(target)))

d2 = Document(target)
n_fig = sum(1 for p in d2.paragraphs for r in p.runs if r._element.findall(qn("w:drawing")))
print("段落 %d，表格 %d，图片 %d" % (len(d2.paragraphs), len(d2.tables), n_fig))
