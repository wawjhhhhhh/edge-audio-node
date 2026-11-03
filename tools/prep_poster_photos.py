# -*- coding: utf-8 -*-
"""准备海报用的实物图：圆角卡片 + 投影，以及抠图后的 ESP32 模块。

三张照片的情况不一样，处理方式也不同（这是刻意的，不是偷懒）：
* board_overview.jpg —— 镜头贴着板子拍，整幅画面都是板子本身，没有可抠的背景；
  只做紧裁 + 圆角 + 投影，避免通用分割模型把排线/边缘啃掉。
* dac_module.jpg —— 手持解码板，手指与板子连成一体，分割模型会把手指当背景切掉；
  同样只做紧裁 + 圆角 + 投影（手是"在演示"的一部分）。
* photo_module.jpg —— 亮面包板 + ESP32 放在深色垫子上，背景可分割；
  用 rembg(u2net) 抠出 ESP32 与音频模块，白底透明 PNG。

输出到 report/figures/subject/：board_card.png / dac_card.png / module_cut.png
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

sys.stdout.reconfigure(encoding="utf-8")

FIG = r"C:\Users\温先生\Desktop\提交内容\edge-audio-node\report\figures"
OUT = os.path.join(FIG, "subject")
os.makedirs(OUT, exist_ok=True)


def rounded(im: Image.Image, radius: int, shadow=18, margin=26) -> Image.Image:
    """给图片加圆角与柔和投影，返回带透明边距的 RGBA。"""
    w, h = im.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    card = Image.new("RGBA", (w + margin * 2, h + margin * 2), (0, 0, 0, 0))
    # 投影
    sh = Image.new("RGBA", card.size, (0, 0, 0, 0))
    sh.paste(Image.new("RGBA", (w, h), (40, 46, 56, 90)), (margin, margin + 6), mask)
    sh = sh.filter(ImageFilter.GaussianBlur(shadow))
    card.alpha_composite(sh)
    # 圆角图
    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    body.paste(im.convert("RGBA"), (0, 0), mask)
    card.alpha_composite(body, (margin, margin))
    return card


def flat(name):
    """读图并压到白底（包里的照片其实是 PNG 字节、文件名带 .jpg，PIL 自己会识别）。"""
    im = Image.open(os.path.join(FIG, name))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg
    return im.convert("RGB")


def main():
    board = flat("board_overview.jpg")
    board = board.crop((6, 6, board.width - 6, board.height - 4))
    rounded(board, 28).save(os.path.join(OUT, "board_card.png"))

    dac = flat("dac_module.jpg")
    w, h = dac.size
    dac = dac.crop((int(w * 0.23), int(h * 0.03), int(w * 0.96), int(h * 0.92)))
    rounded(dac, 30).save(os.path.join(OUT, "dac_card.png"))

    src = os.path.join(FIG, "cut", "photo_module_subject.png")
    if os.path.exists(src):
        m = Image.open(src).convert("RGBA")
        m.save(os.path.join(OUT, "module_cut.png"))
        print("module_cut", m.size)
    for f in ("board_card.png", "dac_card.png"):
        p = os.path.join(OUT, f)
        print(f, Image.open(p).size, os.path.getsize(p))


if __name__ == "__main__":
    main()
