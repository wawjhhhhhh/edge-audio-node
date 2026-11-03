# -*- coding: utf-8 -*-
"""按赛事官方海报模板（AMD_poster_template.pptx）生成中文版与英文版海报，并导出 PDF。

设计原则
--------
* 直接以官方模板为母本：页面尺寸 33.87×19.05 cm、AMD 标识、黑色标题带、竖排装饰字、页脚
  全部沿用模板，只替换内容形状的**文字**与**图片**，因此版式与组委会要求一致。
* 模板里的黄色提示框（"作品实物缩略图""校名""背景，关键信息摘要"…）全部删掉，
  其中两处改写成真正的图注 / 平台信息。
* 中文版把 Aharoni/Calibri 换成微软雅黑（Aharoni 无中文字形），英文版保留模板原字体。
* 图片按原框位置摆，保持长宽比不拉伸；框图换成专门为海报重画的大字号版本。

用法：
    python build_poster.py            # 生成 poster_zh.pptx / poster_en.pptx
    python build_poster.py --pdf      # 顺带用 WPS 导出同名 PDF
"""
import os
import shutil
import sys

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Cm, Pt

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
TPL = r"C:\Users\温先生\xwechat_files\wxid_jl6ijull90e122_2d40\msg\file\2026-11\AMD_poster_template (1).pptx"
FIG = os.path.join(PKG, "report", "figures")
OUTDIR = os.path.join(PKG, "poster")

INK = RGBColor(0x1A, 0x1A, 0x1A)
BLACK = RGBColor(0x00, 0x00, 0x00)
RED = RGBColor(0xD0, 0x21, 0x2E)
GREY = RGBColor(0x55, 0x5F, 0x6B)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

# 模板里的黄色提示语；命中即删（标题框单独处理，不在此列）
HINT_KEYS = [
    "Font: Calibri", "Team Member Names Font", "校徽", "校名",
    "作品实物缩略图", "作品测试平台信息",
    "系统框图，关键创新点", "测试结果，性能，展望", "背景，关键信息摘要",
]

ZH = dict(
    title="基于 ZYNQ-7020 的实时音频采集、降噪与双路回放系统",
    authors="参赛队员：温俊浩　林子源　姜远",
    affil="参赛单位：（待填写）",
    band="引  言",
    intro=[
        "本作品把一条完整的音频链路装进一块 ZYNQ-7020：ES8388 采集的信号经 PL 内 I²S 与异步 FIFO 跨时钟搬运，"
        "由 AXIS + DMA 写入 DDR，ARM 端按 10 ms 定长帧以 UDP 上行到 PC；回放原路返回，同一组 I²S 扇出到板载 "
        "与外接两片 DAC。",
        "硬实时留给硬件：BCLK/LRCLK 由 PL 产生，采样与合帧全是纯状态机，软件只管协议与调度。",
    ],
    case_head="典型应用场景",
    case=[
        "教学与竞赛：模拟前端、时钟、跨时钟域、DMA、网络协议，每一层都能用示波器与读数验证。",
        "嵌入式音频产品前期验证：麦克风阵列、会议终端、录音笔都需要一条可改、可测、可回放的采集链路。",
        "工业与安防声学监测：设备异响、管道泄漏要的是长期稳定上行与本地可判，定长帧与可观测丢包正好对应。",
    ],
    issues_head="关键问题与做法：",
    issues=[
        "① 采集与跨时钟域不能有 CPU 参与 → PL 内 I²S 状态机 + 格雷码指针异步 FIFO。",
        "② 100 帧/秒无损上行 → 480 字定长帧、每帧拆两个 968 B 的 UDP 载荷、DMA 双缓冲。",
        "③ 排除软件变量的音质对比 → 两片 DAC 共享同一组 BCK/LRCR/DATA。",
        "④ 降噪能否留在 ARM → 用实测时延回答：噪声门 0.127 ms/帧，RNNoise 20.07 ms/帧，后者已越线。",
    ],
    arch_head="系统架构",
    box_pl=("PL 硬件采集通路：", [
        "1. I²S 主机时序：BCLK 1.536 MHz、LRCLK 48 kHz、每帧 32 BCLK；",
        "2. BCLK 域 16 bit 移位、左右声道合帧；",
        "3. 格雷码指针异步 FIFO 跨到 100 MHz 域；",
        "4. AXIS → 时钟转换 → DMA S2MM 入 DDR。",
    ]),
    box_ps=("PS 帧调度与网络：", [
        "1. 10 ms 帧循环，480 字（1 920 字节）/帧；",
        "2. 每帧拆两包：8 B 头 + 960 B，100 帧/秒；",
        "3. DMA 双缓冲吸收网络抖动；",
        "4. MIC 看门狗 200 ms，采集停摆自动重配 codec。",
    ]),
    right=[
        ("实测（真实硬件）：", 11, True, RED),
        ("100.0 帧/秒、1.549 Mbit/s、丢包 0、欠载 2、输入 −31.2 dBFS；控制台每 500 ms 刷新波形、频谱与遥测。"
         "资源：LUT 19 656（36.95%）、FF 48 299（45.39%）、BRAM 8、DSP 0，WNS +0.357 ns，功耗 2.189 W。",
         10, False, INK),
        ("两点要说清楚：", 11, True, RED),
        ("双 DAC 并联是\"零软件变量\"的实验台；但布局密度已达 99.40%，因为异步 FIFO 的存储阵列被综合成触发器"
         "（16 384×2）。改为块 RAM 可释放约 32 800 个触发器，谱减也已具备搬进 PL 的条件。",
         10, False, INK),
    ],
    photo_cap="系统实物：ZYNQ-7020 板 + 外接解码板 + ESP32-S3",
    plat_cap="平台：Windows 11 / Python 3.12 / 控制台 :8770",
    onboard="AMD ZYNQ-7020 板上实测",
)

EN = dict(
    title="Real-Time Audio Capture, Denoising and Dual-DAC Playback on ZYNQ-7020",
    authors="Team members: Junhao Wen, Ziyuan Lin, Yuan Jiang",
    affil="Affiliation: (to be filled)",
    band="INTRODUCTION",
    intro=[
        "One ZYNQ-7020 carries the whole chain: an ES8388 captures audio, an I²S state machine and an "
        "asynchronous FIFO move it inside the PL, AXIS and DMA write DDR, and the ARM core sends fixed 10 ms "
        "frames over UDP to the PC.",
        "Playback returns the same way, fanned out from one I²S bus to the on-board ES8388 and an external "
        "ES9038Q2M. The software only handles protocol and scheduling.",
    ],
    case_head="Typical application case",
    case=[
        "Teaching and competitions: analog front end, clocks, clock-domain crossing, DMA and networking - each "
        "layer can be verified with a scope and with counters.",
        "Early validation of embedded audio products: microphone arrays, conference terminals and recorders all "
        "need a capture path that can be modified, measured and replayed.",
        "Acoustic monitoring in industry and security: long, stable uplinks plus local observability, which is "
        "what fixed frames and visible packet loss provide.",
    ],
    issues_head="Key technical issues addressed:",
    issues=[
        "1. Capture and clock-domain crossing without CPU - an I²S state machine plus a gray-code pointer "
        "asynchronous FIFO inside the PL.",
        "2. Lossless 100 fps uplink - 480-word frames split into two 968 B UDP payloads, double-buffered DMA.",
        "3. Audio comparison without software variables - both DACs share the same BCK/LRCR/DATA bus.",
        "4. Can denoising stay on the ARM? - measured: a noise gate costs 0.127 ms per frame, RNNoise 20.07 ms.",
    ],
    arch_head="System Architecture",
    box_pl=("PL hardware capture path:", [
        "1. I²S master timing: BCLK 1.536 MHz, LRCLK 48 kHz, 32 BCLK per frame;",
        "2. 16-bit shift and L/R framing in the BCLK domain;",
        "3. Gray-code pointer asynchronous FIFO into the 100 MHz domain;",
        "4. AXIS and clock converter, then DMA S2MM into DDR.",
    ]),
    box_ps=("PS frame scheduling and networking:", [
        "1. 10 ms frame loop, 480 words (1 920 bytes) per frame;",
        "2. Two UDP packets per frame: 8 B header + 960 B, 100 fps;",
        "3. Double-buffered DMA absorbs network jitter;",
        "4. 200 ms microphone watchdog re-initialises the codec.",
    ]),
    right=[
        ("Measured on hardware:", 11, True, RED),
        ("100.0 fps, 1.549 Mbit/s, 0 packet loss, 2 underruns, -31.2 dBFS input; the console refreshes waveform, "
         "spectrum and telemetry every 500 ms. LUT 19 656 (36.95%), FF 48 299 (45.39%), BRAM 8, DSP 0, "
         "WNS +0.357 ns, power 2.189 W.", 10, False, INK),
        ("Two points worth stating:", 11, True, RED),
        ("The dual-DAC fan-out gives a bench with no software variable; slice utilisation is already 99.40% "
         "because the asynchronous FIFO arrays are inferred as registers (16 384 x 2). Moving them to block RAM "
         "frees about 32 800 flip-flops, and the spectral-subtraction core is ready for the PL.", 10, False, INK),
    ],
    photo_cap="Hardware: ZYNQ-7020 board, external DAC and ESP32-S3",
    plat_cap="Platform: Windows 11 / Python 3.12 / console :8770",
    onboard="Measured on AMD ZYNQ-7020",
)


# ------------------------------------------------------------------ 工具
def set_text(shape, blocks, font, size=10, color=INK, align=PP_ALIGN.LEFT,
             spacing=1.05, space_after=2, anchor=MSO_ANCHOR.TOP, bold=False):
    tf = shape.text_frame
    tf.word_wrap = True
    tf.clear()
    tf.vertical_anchor = anchor
    tf.margin_left = Cm(0.10)
    tf.margin_right = Cm(0.10)
    tf.margin_top = Cm(0.03)
    tf.margin_bottom = Cm(0.03)
    first = True
    for b in blocks:
        if isinstance(b, str):
            text, sz, bd, col = b, None, None, None
        else:
            text, sz, bd, col = (list(b) + [None, None, None])[:4]
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.alignment = align
        p.line_spacing = spacing
        p.space_after = Pt(space_after)
        r = p.add_run()
        r.text = text
        r.font.size = Pt(sz or size)
        r.font.bold = bold if bd is None else bool(bd)
        r.font.color.rgb = col or color
        r.font.name = font
        rPr = r._r.get_or_add_rPr()
        ea = rPr.find(qn("a:ea"))
        if ea is None:
            ea = rPr.makeelement(qn("a:ea"), {})
            rPr.append(ea)
        ea.set("typeface", font)


def add_box(slide, x, y, w, h, blocks, font, **kw):
    sh = slide.shapes.add_textbox(Cm(x), Cm(y), Cm(w), Cm(h))
    set_text(sh, blocks, font, **kw)
    return sh


def _parent(shape):
    return shape._element.getparent()


def delete(shape):
    el = shape._element
    el.getparent().remove(el)


def replace_pic(slide, shape, path, mode="width"):
    """保持原形状的层级与位置，换成 path 指向的图片，按比例缩放。"""
    x, y, w, h = shape.left, shape.top, shape.width, shape.height
    iw, ih = Image.open(path).size
    ar = iw / ih
    if mode == "width":
        nw, nh = w, int(w / ar)
    elif mode == "height":
        nh, nw = h, int(h * ar)
    else:
        if w / h > ar:
            nh, nw = h, int(h * ar)
        else:
            nw, nh = w, int(w / ar)
    cx = x + (w - nw) // 2
    cy = y + (h - nh) // 2
    parent = _parent(shape)
    idx = list(parent).index(shape._element)
    delete(shape)
    pic = slide.shapes.add_picture(path, cx, cy, nw, nh)
    el = pic._element
    el.getparent().remove(el)
    parent.insert(idx, el)
    return pic


# ------------------------------------------------------------------ 生成
def build(lang, C):
    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "_stage_%s.pptx" % lang)
    shutil.copyfile(TPL, out)

    prs = Presentation(out)
    s = prs.slides[0]
    fb = "微软雅黑" if lang == "zh" else "Calibri"
    fh = "微软雅黑" if lang == "zh" else "Aharoni"
    ft = "Times New Roman"

    # 0) 母版里的版权行
    for sh in s.slide_layout.slide_master.shapes:
        if sh.has_text_frame and "Copyright" in sh.text_frame.text:
            set_text(sh, [("© 2026", 9, False, GREY)], fb, spacing=1.0)

    # 1) 提示框：两处改写成图注，其余删除
    for sh in list(s.shapes):
        if not sh.has_text_frame:
            continue
        t = sh.text_frame.text.strip()
        if not t or sh.name == "TextBox 8":
            continue
        if any(k in t for k in HINT_KEYS):
            if "作品实物缩略图" in t:
                sh.top, sh.left, sh.width = Cm(3.12), Cm(28.55), Cm(5.2)
                set_text(sh, [(C["photo_cap"], 8.5, False, GREY)], fb, spacing=1.0)
            elif "作品测试平台信息" in t:
                sh.top, sh.left, sh.width = Cm(4.88), Cm(28.55), Cm(5.2)
                set_text(sh, [(C["plat_cap"], 8.5, False, GREY)], fb, spacing=1.0)
            else:
                delete(sh)

    # 2) 标题 / 队员 / 单位
    for sh in s.shapes:
        if not sh.has_text_frame:
            continue
        t = sh.text_frame.text.strip()
        if sh.name == "TextBox 8":
            sh.left, sh.top, sh.width, sh.height = Cm(0.55), Cm(1.12), Cm(13.9), Cm(1.85)
            set_text(sh, [(C["title"], 21 if lang == "zh" else 20, True, BLACK)], ft,
                     spacing=1.02, anchor=MSO_ANCHOR.MIDDLE)
        elif t.startswith("Hao Zhang"):
            sh.left, sh.top, sh.width, sh.height = Cm(18.05), Cm(0.62), Cm(9.55), Cm(0.85)
            set_text(sh, [(C["authors"], 12 if lang == "zh" else 11, True, BLACK)], ft,
                     spacing=1.0, align=PP_ALIGN.CENTER)
        elif "China Three Gorges" in t:
            # 单位不再用文字，改成"校徽 + 校名"的整枚 logo（白字透明底，
            # 这里用 make_poster_assets.py 生成的深色版），位置仍在右上角
            delete(sh)

    # 3) 黑色标题带
    for sh in s.shapes:
        if sh.name == "Title 1" and sh.has_text_frame and sh.text_frame.text.strip() == "INTRODUCTION":
            set_text(sh, [(C["band"], 20 if lang == "zh" else 18, True, WHITE)], fh,
                     align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

    # 4) 小标题
    for sh in s.shapes:
        if not sh.has_text_frame:
            continue
        t = sh.text_frame.text.strip()
        if t == "Typical application case":
            set_text(sh, [(C["case_head"], 11, True, BLACK)], fb, align=PP_ALIGN.CENTER, spacing=1.0)
        elif t == "System Architecture":
            sh.top = Cm(7.55)
            set_text(sh, [(C["arch_head"], 12, True, BLACK)], fb, align=PP_ALIGN.CENTER, spacing=1.0)
        elif "On board test by AMD" in t:
            sh.left, sh.top = Cm(27.9), Cm(4.35)
            set_text(sh, [(C["onboard"], 10, True, INK)], fb, spacing=1.0)

    # 5) 正文：拆成"引言"与"应用场景 + 关键问题"两个受控文本框
    for sh in list(s.shapes):
        if sh.has_text_frame and sh.text_frame.text.strip().startswith("Radio telescopes"):
            delete(sh)

    add_box(s, 0.55, 4.86, 10.05, 2.36, [(p, 9 if lang == "zh" else 8.6, False, INK) for p in C["intro"]],
            fb, spacing=1.00, space_after=2)

    blocks = [(p, 9.5, False, INK) for p in C["case"]]
    blocks.append(("", 4, False, INK))
    blocks.append((C["issues_head"], 9.5, True, BLACK))
    blocks += [(p, 9.5, False, INK) for p in C["issues"]]
    add_box(s, 0.55, 9.55, 10.05, 7.90, blocks, fb, spacing=1.02, space_after=2)

    # 6) 圆角框：上=PL 采集通路，下=PS 调度与网络
    for sh in s.shapes:
        if not sh.has_text_frame:
            continue
        t = sh.text_frame.text.strip()
        if t.startswith("High-speed Acquisition Card"):
            title, items = C["box_pl"]
            sh.top, sh.height = Cm(8.90), Cm(3.45)
            set_text(sh, [(title, 11, True, RED)] + [(x, 9.5, False, INK) for x in items],
                     fb, spacing=1.02, space_after=1)
        elif t.startswith("Optical Communication Receiver Card"):
            title, items = C["box_ps"]
            sh.top, sh.height = Cm(12.70), Cm(3.95)
            set_text(sh, [(title, 11, True, RED)] + [(x, 9.5, False, INK) for x in items],
                     fb, spacing=1.02, space_after=1)

    # 7) 右侧两段（原来是 SFP+ / 存储带宽）
    for sh in s.shapes:
        if sh.has_text_frame and sh.text_frame.text.strip().startswith("SFP+ interface"):
            set_text(sh, C["right"], fb, size=10, spacing=1.04, space_after=5)

    # 8) 图片
    for sh in list(s.shapes):
        if sh.shape_type != 13:
            continue
        if sh.name == "图片 7":
            pic = replace_pic(s, sh, os.path.join(FIG, "strip_scene.png"), mode="width")
            pic.left, pic.top, pic.width = Cm(1.90), Cm(7.32), Cm(7.40)
            iw, ih = Image.open(os.path.join(FIG, "strip_scene.png")).size
            pic.height = int(pic.width * ih / iw)
        elif sh.name == "图片 2":
            arch = "fig_poster_arch.png" if lang == "zh" else "fig_poster_arch_en.png"
            pic = replace_pic(s, sh, os.path.join(FIG, arch), mode="width")
            pic.left, pic.top, pic.width = Cm(11.40), Cm(3.05), Cm(9.05)
            iw, ih = Image.open(os.path.join(FIG, arch)).size
            pic.height = int(pic.width * ih / iw)
        elif sh.name == "图片 5":
            th = os.path.join(FIG, "thumb_board.png")
            pic = replace_pic(s, sh, th, mode="width")
            iw, ih = Image.open(th).size
            hh = 2.86
            pic.height = Cm(hh)
            pic.width = Cm(hh * iw / ih)
            pic.left, pic.top = Cm(29.10), Cm(0.16)
        elif sh.name in ("Picture 11", "Picture 38"):
            delete(sh)   # 校徽 + 一个隐藏占位图：本作品不放学校信息

    # 校徽与校名分开摆（两枚素材各自独立，位置可单独微调）
    badge = os.path.join(FIG, "logo_badge_dark.png")
    name = os.path.join(FIG, "logo_name_dark.png")
    if os.path.exists(badge) and os.path.exists(name):
        bi = Image.open(badge)
        ni = Image.open(name)
        # 校徽：正方形，1.62 cm；校名：字幅 7.05 cm（英文版略窄）
        bw = 1.78
        bh = bw * bi.height / bi.width
        nw = 7.05 if lang == "zh" else 6.70
        nh = nw * ni.height / ni.width
        # 校名在上、校徽在下：校徽居中挂在校名正下方，填掉那块空白
        cy = 2.86                       # 校名的底边
        nx = 18.15
        s.shapes.add_picture(name, Cm(nx), Cm(cy - nh), width=Cm(nw), height=Cm(nh))
        s.shapes.add_picture(badge, Cm(nx + nw - bw), Cm(cy + 0.26), width=Cm(bw), height=Cm(bh))

    prs.save(out)
    final = os.path.join(OUTDIR, "poster_%s.pptx" % lang)
    try:
        os.replace(out, final)
        out = final
    except PermissionError:
        # 目标被 PowerPoint/WPS/预览占用：保留 _stage_ 文件，稍后由 swap 脚本替换
        print("LOCKED: keep", os.path.basename(out))
    print("saved", out, os.path.getsize(out))
    return out


def export_pdf(pptx_path):
    import comtypes.client
    pdf = os.path.splitext(pptx_path)[0] + ".pdf"
    app = comtypes.client.CreateObject("KWPP.Application")
    try:
        app.Visible = False
    except Exception:
        pass
    pres = app.Presentations.Open(pptx_path, WithWindow=False)
    tmp = os.path.join(os.path.dirname(pdf), "_export_tmp.pdf")
    if os.path.exists(tmp):
        os.remove(tmp)
    pres.ExportAsFixedFormat(tmp, 2)
    pres.Close()
    stage = os.path.join(os.path.dirname(pdf), "_stage_%s.pdf" % os.path.splitext(os.path.basename(pdf))[0].split("_")[-1])
    try:
        os.replace(tmp, pdf)
    except PermissionError:
        os.replace(tmp, stage)
        pdf = stage
    print("pdf  ", pdf, os.path.getsize(pdf))
    return pdf


if __name__ == "__main__":
    outs = [build("zh", ZH), build("en", EN)]
    if "--pdf" in sys.argv:
        for p in outs:
            export_pdf(p)
