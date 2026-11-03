# -*- coding: utf-8 -*-
"""生成海报用的两张插图：中/英文系统框图，以及三张实物照片拼成的横幅。

框图刻意画得很"粗"：2 行 × 4 列、字号按框宽自动收缩，缩到 A4 横向 9 cm 宽仍然可读
（报告里的 fig1_system.png 太细，缩到这个尺寸会看不清）。
"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
FIG = os.path.join(os.path.dirname(HERE), "report", "figures")

INK = (28, 34, 42)
SUB = (92, 102, 115)
BLUE = (30, 90, 220)
AMBER = (176, 78, 8)
GREEN = (12, 122, 84)
FILL_PL = (234, 241, 255)
FILL_PS = (231, 247, 239)
FILL_END = (246, 246, 250)
EDGE_END = (126, 136, 150)
FB = r"C:\Windows\Fonts\msyhbd.ttc"
FR = r"C:\Windows\Fonts\msyh.ttc"

ZH = dict(
    up="上行：采集 → 板载处理 → 网络",
    down="回放：网络 → 板载处理 → 双 DAC",
    rows=[
        [("ES8388 采集", ["MIC / 3.5 mm", "48 kHz · 16 bit"], FILL_END, EDGE_END),
         ("PL：I²S 收 + FIFO", ["BCLK 1.536 MHz", "跨到 100 MHz 域"], FILL_PL, BLUE),
         ("PS：10 ms 定长帧", ["480 字 / 帧", "UDP 50505 · 100 fps"], FILL_PS, GREEN),
         ("PC 控制台", ["波形 · 频谱 · 遥测", "每 500 ms 刷新"], FILL_END, EDGE_END)],
        [("ES8388 / ES9038", ["板载 + 外接解码", "同源 · 两个耳机口"], FILL_END, EDGE_END),
         ("PL：I²S 发（同源）", ["同一组 BCK/LRCR", "/DATA 并行送出"], FILL_PL, BLUE),
         ("PS：DMA 双缓冲", ["一块在发", "一块在填"], FILL_PS, GREEN),
         ("PC → 板 PCM", ["UDP 50506", "控制 50507"], FILL_END, EDGE_END)],
    ],
)

EN = dict(
    up="Uplink: capture → on-board → network",
    down="Playback: network → on-board → dual DAC",
    rows=[
        [("ES8388 capture", ["MIC / 3.5 mm", "48 kHz · 16 bit"], FILL_END, EDGE_END),
         ("PL: I²S RX + FIFO", ["BCLK 1.536 MHz", "into 100 MHz domain"], FILL_PL, BLUE),
         ("PS: 10 ms frames", ["480 words / frame", "UDP 50505 · 100 fps"], FILL_PS, GREEN),
         ("PC console", ["waveform · spectrum", "refresh every 500 ms"], FILL_END, EDGE_END)],
        [("ES8388 / ES9038", ["on-board + external", "two jacks, one bus"], FILL_END, EDGE_END),
         ("PL: I²S TX (same bus)", ["shared BCK/LRCR", "/DATA fan-out"], FILL_PL, BLUE),
         ("PS: DMA double buffer", ["one fills,", "one sends"], FILL_PS, GREEN),
         ("PC → board PCM", ["UDP 50506", "control 50507"], FILL_END, EDGE_END)],
    ],
)


def draw_arch(spec, out):
    W, H = 1600, 720
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    cache = {}

    def fnt(size, bold=False):
        k = (size, bold)
        if k not in cache:
            try:
                cache[k] = ImageFont.truetype(FB if bold else FR, size)
            except Exception:
                cache[k] = ImageFont.load_default()
        return cache[k]

    BW, BH, GAP = 336, 150, 44
    X0 = (W - (4 * BW + 3 * GAP)) // 2
    Y1, Y2 = 108, 448

    def fit(cx, y, text, max_w, size, bold=False, fill=INK):
        while size > 14:
            f = fnt(size, bold)
            if d.textlength(text, font=f) <= max_w:
                break
            size -= 2
        f = fnt(size, bold)
        d.text((cx - d.textlength(text, font=f) / 2, y), text, font=f, fill=fill)

    def box(x, y, title, subs, fill, edge):
        d.rounded_rectangle([x, y, x + BW, y + BH], radius=16, fill=fill, outline=edge, width=3)
        fit(x + BW / 2, y + 20, title, BW - 26, 38, True)
        yy = y + 74
        for s in subs:
            fit(x + BW / 2, yy, s, BW - 26, 26, False, SUB)
            yy += 32

    def arrow(x1, y1, x2, y2, width=5):
        d.line([x1, y1, x2, y2], fill=INK, width=width)
        ang = math.atan2(y2 - y1, x2 - x1)
        for s in (+0.45, -0.45):
            d.line([x2, y2, x2 - 20 * math.cos(ang + s), y2 - 20 * math.sin(ang + s)], fill=INK, width=width)

    for y, label, color, items in ((Y1, spec["up"], BLUE, spec["rows"][0]),
                                   (Y2, spec["down"], AMBER, spec["rows"][1])):
        d.text((X0, y - 44), label, font=fnt(28, True), fill=color)
        for i, (t, subs, fill, edge) in enumerate(items):
            box(X0 + i * (BW + GAP), y, t, subs, fill, edge)

    for i in range(3):
        xs = X0 + i * (BW + GAP) + BW
        arrow(xs + 7, Y1 + BH // 2, xs + GAP - 9, Y1 + BH // 2)
        xt = X0 + (3 - i) * (BW + GAP)
        arrow(xt - 7, Y2 + BH // 2, xt - GAP + 9, Y2 + BH // 2)

    mid = X0 + 2 * (BW + GAP) + BW // 2
    d.line([mid, Y1 + BH + 12, mid, Y2 - 12], fill=(205, 210, 218), width=3)
    fit(mid, (Y1 + BH + Y2) // 2 - 20, "DDR", 120, 24, False, SUB)

    im.save(out)
    print(out, im.size, os.path.getsize(out))


def draw_strip(out):
    srcs = ["board_overview.jpg", "dac_module.jpg", "photo_module.jpg"]
    H, GAP = 300, 8
    ims = []
    for s in srcs:
        im = Image.open(os.path.join(FIG, s)).convert("RGB")
        ims.append(im.resize((int(im.width * H / im.height), H), Image.LANCZOS))
    total = sum(i.width for i in ims) + GAP * (len(ims) - 1)
    canvas = Image.new("RGB", (total, H), "white")
    x = 0
    for i in ims:
        canvas.paste(i, (x, 0))
        x += i.width + GAP
    canvas.save(out)
    print(out, canvas.size, os.path.getsize(out))


if __name__ == "__main__":
    draw_arch(ZH, os.path.join(FIG, "fig_poster_arch.png"))
    draw_arch(EN, os.path.join(FIG, "fig_poster_arch_en.png"))
    draw_strip(os.path.join(FIG, "strip_scene.png"))
