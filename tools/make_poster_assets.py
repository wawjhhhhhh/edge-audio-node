# -*- coding: utf-8 -*-
"""生成海报用的三个素材：

1. `logo_school_dark.png` —— 校徽 + 校名（原图是白色透明底，白底海报上看不见，
   这里把字形重着色成深灰，保留原始 alpha，边缘不毛）。
2. `strip_scene.png` —— 左列三张实物图的横幅（圆角卡 + 白间隙，等高）。
3. `thumb_board.png` —— 右上角"作品实物"缩略图（整板卡片，带白边）。

输入：`subject/board_card.png`、`subject/dac_card.png`、`subject/module_cut.png`
以及用户提供的校徽素材。
"""
import os
import sys

from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")

FIG = r"C:\Users\温先生\Desktop\提交内容\edge-audio-node\report\figures"
SUB = os.path.join(FIG, "subject")
OUT_STRIP = os.path.join(FIG, "strip_scene.png")
OUT_THUMB = os.path.join(FIG, "thumb_board.png")
OUT_BADGE = os.path.join(FIG, "logo_badge_dark.png")
OUT_NAME = os.path.join(FIG, "logo_name_dark.png")

BADGE_SRC = (r"C:\Users\温先生\.dsh\attachments\v1\objects\72"
             r"\726cb51bb66495000c7a402906cf3e2cbaa47d169d2885c1b9c2b28f8d0cdd1c")
NAME_SRC = (r"C:\Users\温先生\.dsh\attachments\v1\objects\8d"
            r"\8d2567cad2ae515919d3875065640b342331f538553c9dccb56de97cb62024c2")
DARK = (31, 41, 55)  # #1F2937


def _recolor(src: str, scale: int, dst: str) -> None:
    """白色透明底 -> 深灰透明底，按 alpha 裁掉空白，再放大以便印刷。"""
    im = Image.open(src).convert("RGBA")
    a = im.getchannel("A")
    out = Image.new("RGBA", im.size, DARK + (0,))
    out.putalpha(a)
    bbox = out.getbbox()
    if bbox:
        out = out.crop(bbox)
    out = out.resize((out.width * scale, out.height * scale), Image.LANCZOS)
    out.save(dst)
    print(os.path.basename(dst), out.size, os.path.getsize(dst))


def make_logos() -> None:
    # 校徽与校名分开放：校徽按正方形位置摆，校名按字幅摆，互不牵制
    _recolor(BADGE_SRC, 6, OUT_BADGE)
    _recolor(NAME_SRC, 4, OUT_NAME)



def _card(img: Image.Image, box=(330, 246), radius: int = 16, shadow: int = 10) -> Image.Image:
    from PIL import ImageDraw, ImageFilter

    w, h = box
    canvas = Image.new("RGBA", (w + 24, h + 24), (255, 255, 255, 0))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sh.paste(Image.new("RGBA", (w, h), (40, 46, 56, 70)), (12, 15), mask)
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(shadow)))
    body = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    body.alpha_composite(img, (0, 0))
    canvas.alpha_composite(body, (12, 12))
    return canvas


def _cover(img: Image.Image, box) -> Image.Image:
    w, h = box
    s = max(w / img.width, h / img.height)
    im = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    return im.crop(((im.width - w) // 2, (im.height - h) // 2,
                    (im.width - w) // 2 + w, (im.height - h) // 2 + h))


def make_strip(height: int = 246, gap: int = 10) -> None:
    """三张统一卡片：两张实拍铺满卡片，抠好的 ESP32 放在浅灰卡片上。"""
    box = (330, height)
    tiles = []
    for name in ("board_overview.jpg", "dac_module.jpg"):
        raw = Image.open(os.path.join(FIG, name))
        if raw.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGBA", raw.size, (255, 255, 255, 255))
            bg.alpha_composite(raw.convert("RGBA"))
            raw = bg
        tiles.append(_card(_cover(raw.convert("RGB").convert("RGBA"), box)))
    cut = Image.open(os.path.join(SUB, "module_cut.png")).convert("RGBA")
    panel = Image.new("RGBA", box, (243, 245, 248, 255))
    s = min((box[0] - 30) / cut.width, (box[1] - 22) / cut.height)
    c = cut.resize((round(cut.width * s), round(cut.height * s)), Image.LANCZOS)
    panel.alpha_composite(c, ((box[0] - c.width) // 2, (box[1] - c.height) // 2))
    tiles.append(_card(panel))

    total = sum(t.width for t in tiles) + gap * (len(tiles) - 1)
    strip = Image.new("RGBA", (total, tiles[0].height), (255, 255, 255, 255))
    x = 0
    for t in tiles:
        strip.alpha_composite(t, (x, 0))
        x += t.width + gap
    strip.convert("RGB").save(OUT_STRIP)
    print("strip", strip.size, os.path.getsize(OUT_STRIP))



def make_thumb(size=(900, 790)) -> None:
    card = Image.open(os.path.join(SUB, "board_card.png")).convert("RGBA")
    canvas = Image.new("RGBA", size, (255, 255, 255, 255))
    scale = min((size[0] - 20) / card.width, (size[1] - 20) / card.height)
    card = card.resize((round(card.width * scale), round(card.height * scale)), Image.LANCZOS)
    canvas.alpha_composite(card, ((size[0] - card.width) // 2, (size[1] - card.height) // 2))
    canvas.convert("RGB").save(OUT_THUMB)
    print("thumb", canvas.size, os.path.getsize(OUT_THUMB))


if __name__ == "__main__":
    make_logos()
    make_strip()
    make_thumb()
