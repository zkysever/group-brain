"""封面实验室：一口气出十版封面模板。

规矩（都是这一个月攒下来的）：
- 画布 2560×1440（16:9），但标题与主角必须落在 4:3 安全区里：x ∈ [320, 2240]。
- 每版都要有表情包本体，不是画个圈占位。
- 文字内容先不管：这里全部写占位词（「主标题写这里」这种），
  位置和字号就是将来真标题的位置，换字不用调版。
- 深色为主，一版一个主色，别都一个味。

用法：
    python tools/cover_lab.py            # 出十版 + 一张总览
    python tools/cover_lab.py 3          # 只出第 3 版
"""

from __future__ import annotations

import math
import sys
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 2560, 1440
SAFE_L, SAFE_R = 320, 2240
OUT = Path(__file__).parent.parent / "out" / "covers"

STICKER_ROOTS = [
    Path(r"C:\Users\Administrator\Desktop\space\deepseek表情包"),
    Path(r"C:\Users\Administrator\Documents\dsh-bili-whale\stickers"),
]

F_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
F_SEMI = r"C:\Windows\Fonts\msyh.ttc"
F_SERIF = r"C:\Windows\Fonts\simsun.ttc"
F_MONO = r"C:\Windows\Fonts\consolab.ttf"

INK = (236, 240, 245)
DIM = (150, 160, 172)
FAINT = (96, 104, 116)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


# ---------------------------------------------------------------- 表情包处理
def cut_white(im: Image.Image, thr: int = 232) -> Image.Image:
    """把白底抠掉（从四边泛洪），保留贴纸本身的白描边。"""
    im = im.convert("RGBA")
    w, h = im.size
    alpha = im.getchannel("A")
    if alpha.getextrema()[0] < 250:  # 本来就有透明，直接裁边
        box = alpha.getbbox()
        return im.crop(box) if box else im

    px = im.load()
    keep = bytearray(w * h)
    dq: deque[tuple[int, int]] = deque()
    for x in range(w):
        dq.append((x, 0))
        dq.append((x, h - 1))
    for y in range(h):
        dq.append((0, y))
        dq.append((w - 1, y))
    while dq:
        x, y = dq.pop()
        if x < 0 or y < 0 or x >= w or y >= h:
            continue
        i = y * w + x
        if keep[i]:
            continue
        r, g, b, a = px[x, y]
        if a > 12 and not (r > thr and g > thr and b > thr):
            continue
        keep[i] = 1
        dq.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

    out = im.copy()
    op = out.load()
    for y in range(h):
        row = y * w
        for x in range(w):
            if keep[row + x]:
                r, g, b, _ = op[x, y]
                op[x, y] = (r, g, b, 0)
    box = out.getchannel("A").getbbox()
    return out.crop(box) if box else out


def sticker(path: Path, size: int, outline: int = 10) -> Image.Image:
    """读一张表情包，抠底、加白色贴纸边、缩到 size 以内。"""
    im = Image.open(path)
    try:
        im.seek(0)
    except Exception:
        pass
    im = cut_white(im)
    im.thumbnail((size, size), Image.LANCZOS)

    if outline:
        a = im.getchannel("A")
        grown = a.filter(ImageFilter.MaxFilter(outline * 2 + 1))
        border = Image.new("RGBA", im.size, (255, 255, 255, 0))
        border.putalpha(grown)
        im = Image.alpha_composite(border, im)
    return im


def drop_shadow(layer: Image.Image, blur: int = 18, alpha: int = 120) -> Image.Image:
    """给贴纸加一层柔光阴影，深色背景上才有“贴上去”的立体感。"""
    sh = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    sh.putalpha(layer.getchannel("A").point(lambda v: min(alpha, v)))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    return sh


def paste_sticker(base: Image.Image, st: Image.Image, xy: tuple[int, int], shadow: bool = True) -> None:
    x, y = xy
    if shadow:
        sh = drop_shadow(st)
        base.alpha_composite(sh, (x + 14, y + 22))
    base.alpha_composite(st, (x, y))


def pick_stickers(n: int, min_side: int = 480) -> list[Path]:
    """挑 n 张够大、能放大的表情包（不同分类，尽量不重样）。"""
    pools: dict[str, list[Path]] = {}
    for root in STICKER_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() not in (".png", ".gif", ".webp"):
                continue
            if "预览" in p.name or "总览" in p.name:
                continue
            try:
                with Image.open(p) as im:
                    if min(im.size) < min_side:
                        continue
            except Exception:
                continue
            key = p.parent.name
            pools.setdefault(key, []).append(p)
    out: list[Path] = []
    keys = sorted(pools)
    i = 0
    while len(out) < n and keys:
        k = keys[i % len(keys)]
        bucket = pools[k]
        if bucket:
            out.append(bucket.pop(0))
        i += 1
        if i > 400:
            break
    return out


# ---------------------------------------------------------------- 画布小工具
def canvas(bg=(9, 10, 13), vignette: float = 0.45) -> Image.Image:
    im = Image.new("RGBA", (W, H), bg + (255,))
    return im


def vgrad(top, bottom) -> Image.Image:
    im = Image.new("RGBA", (1, H))
    for y in range(H):
        t = y / (H - 1)
        im.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    return im.resize((W, H))


def glow(base: Image.Image, box, color, blur: int = 90, alpha: int = 90) -> None:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse(box, fill=color + (alpha,))
    base.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def grid(base: Image.Image, step: int = 80, color=(255, 255, 255, 12)) -> None:
    d = ImageDraw.Draw(base)
    for x in range(0, W, step):
        d.line([(x, 0), (x, H)], fill=color)
    for y in range(0, H, step):
        d.line([(0, y), (W, y)], fill=color)


def scanlines(base: Image.Image, alpha: int = 22, step: int = 4) -> None:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for y in range(0, H, step):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, alpha))
    base.alpha_composite(layer)


def vignette(base: Image.Image, strength: int = 150) -> None:
    layer = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(layer)
    d.ellipse([-W * 0.25, -H * 0.35, W * 1.25, H * 1.35], fill=255)
    layer = layer.filter(ImageFilter.GaussianBlur(180))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, strength))
    dark.putalpha(layer.point(lambda v: int((255 - v) * strength / 255)))
    base.alpha_composite(dark)


def safe_guide(im: Image.Image) -> Image.Image:
    """调试用：画出 4:3 安全区（不参与出图）。"""
    d = ImageDraw.Draw(im)
    d.rectangle([SAFE_L, 0, SAFE_R, H], outline=(255, 0, 0, 160), width=4)
    return im


def text(d, xy, s, f, fill, anchor="la", spacing=0):
    d.text(xy, s, font=f, fill=fill, anchor=anchor, spacing=spacing)


def vtext(d, xy, s, f, fill, gap=1.06):
    """竖排：一个字一行。"""
    x, y = xy
    step = int(f.size * gap)
    for ch in s:
        d.text((x, y), ch, font=f, fill=fill, anchor="ma")
        y += step


# ---------------------------------------------------------------- 十版
def c01_terminal(sts: list[Image.Image]) -> Image.Image:
    im = canvas((8, 10, 9))
    glow(im, [-400, 200, 1200, 1500], (40, 200, 120), blur=160, alpha=70)
    grid(im, 64, (60, 255, 150, 10))
    scanlines(im)
    d = ImageDraw.Draw(im)
    # 终端窗
    box = (SAFE_L - 40, 190, 1560, 1150)
    d.rounded_rectangle(box, 26, fill=(12, 16, 14, 235), outline=(70, 255, 160, 70), width=3)
    d.rounded_rectangle([box[0], box[1], box[2], box[1] + 66], 26, fill=(18, 26, 22, 255))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([box[0] + 26 + i * 40, box[1] + 22, box[0] + 46 + i * 40, box[1] + 42], fill=c + (255,))
    text(d, (box[0] + 170, box[1] + 20), "ep00 / 主标题写这里", font(F_MONO, 30), (120, 220, 165, 235))
    text(d, (box[0] + 60, box[1] + 150), "$", font(F_MONO, 62), (90, 255, 165, 255))
    text(d, (box[0] + 120, box[1] + 140), "主标题写这里，两行也行", font(F_BOLD, 92), INK + (255,))
    text(d, (box[0] + 120, box[1] + 262), "第二行副标题", font(F_BOLD, 92), (255, 196, 107, 255))
    text(d, (box[0] + 120, box[1] + 420), "# 一句话说清这期讲什么", font(F_SEMI, 40), (150, 205, 175, 220))
    text(d, (box[0] + 120, box[1] + 500), "# 副标题第二行，放数据或结论", font(F_SEMI, 40), (120, 170, 145, 200))
    d.line([(box[0] + 120, box[1] + 600), (box[0] + 120 + 260, box[1] + 600)], fill=(90, 255, 165, 180), width=6)
    text(d, (box[0] + 120, box[1] + 640), "第 00 期 · 从零做 AI 应用", font(F_SEMI, 38), (120, 190, 155, 230))
    st = sts[0].resize((820, int(820 * sts[0].height / sts[0].width)), Image.LANCZOS)
    paste_sticker(im, st, (1600, 420))
    return im


def c02_poster(sts: list[Image.Image]) -> Image.Image:
    im = canvas((16, 12, 10))
    glow(im, [1400, -300, 2600, 900], (255, 120, 60), blur=180, alpha=80)
    d = ImageDraw.Draw(im)
    # 右侧斜色带
    band = [(1860, 0), (W, 0), (W, H), (2150, H)]
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).polygon(band, fill=(255, 122, 46, 235))
    im.alpha_composite(layer)
    text(d, (SAFE_L, 300), "主标题", font(F_SERIF, 210), INK + (255,))
    text(d, (SAFE_L, 530), "写在这里", font(F_SERIF, 210), (255, 255, 255, 255))
    text(d, (SAFE_L, 800), "副标题一行，说明这期做了什么", font(F_SEMI, 54), (255, 190, 150, 240))
    d.line([(SAFE_L, 900), (SAFE_L + 420, 900)], fill=(255, 140, 70, 255), width=8)
    text(d, (SAFE_L, 960), "第 00 期  ·  从零做 AI 应用", font(F_SEMI, 42), DIM + (255,))
    st = sts[1].resize((900, int(900 * sts[1].height / sts[1].width)), Image.LANCZOS)
    paste_sticker(im, st, (1380, 700))
    return im


def c03_minimal(sts: list[Image.Image]) -> Image.Image:
    im = vgrad((18, 19, 22), (8, 8, 10))
    vignette(im, 120)
    d = ImageDraw.Draw(im)
    d.rectangle([96, 96, W - 96, H - 96], outline=(255, 255, 255, 40), width=2)
    text(d, (W // 2, 430), "主标题写这里", font(F_BOLD, 168), INK + (255,), anchor="ma")
    text(d, (W // 2, 640), "副标题，一句话", font(F_SEMI, 60), DIM + (255,), anchor="ma")
    d.line([(W // 2 - 90, 780), (W // 2 + 90, 780)], fill=(255, 255, 255, 120), width=3)
    text(d, (W // 2, 850), "从零做 AI 应用 · 第 00 期", font(F_SEMI, 38), FAINT + (255,), anchor="ma")
    st = sts[2].resize((640, int(640 * sts[2].height / sts[2].width)), Image.LANCZOS)
    paste_sticker(im, st, (SAFE_R - 700, 830), shadow=False)
    return im


def c04_cinema(sts: list[Image.Image]) -> Image.Image:
    im = canvas((6, 6, 7))
    d = ImageDraw.Draw(im)
    # 上下黑边
    d.rectangle([0, 0, W, 130], fill=(0, 0, 0, 255))
    d.rectangle([0, H - 130, W, H], fill=(0, 0, 0, 255))
    glow(im, [700, 300, 1900, 1200], (60, 90, 255), blur=200, alpha=70)
    text(d, (SAFE_L, 330), "主标题写在", font(F_BOLD, 150), INK + (255,))
    text(d, (SAFE_L, 510), "这里，两行", font(F_BOLD, 150), (150, 190, 255, 255))
    text(d, (SAFE_L, 760), "副标题：一句话交代这期在干什么", font(F_SEMI, 48), (170, 185, 210, 235))
    vtext(d, (SAFE_R - 60, 340), "从零做 AI 应用", font(F_SEMI, 44), (120, 140, 175, 220))
    st = sts[3].resize((760, int(760 * sts[3].height / sts[3].width)), Image.LANCZOS)
    paste_sticker(im, st, (1520, 880))
    text(d, (W // 2, H - 76), "第 00 期", font(F_SEMI, 34), (150, 150, 155, 235), anchor="mm")
    return im


def c05_cards(sts: list[Image.Image]) -> Image.Image:
    im = vgrad((22, 20, 26), (10, 9, 12))
    glow(im, [-200, 600, 900, 1500], (140, 90, 255), blur=180, alpha=60)
    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for k, (dx, col) in enumerate([(0, (30, 28, 38)), (56, (42, 39, 54)), (112, (54, 50, 70))]):
        card = Image.new("RGBA", (980, 620), col + (255,))
        ImageDraw.Draw(card).rounded_rectangle([0, 0, 979, 619], 34, fill=col + (255,),
                                               outline=(255, 255, 255, 40), width=3)
        card = card.rotate(-6, expand=True, resample=Image.BICUBIC)
        base.alpha_composite(card, (SAFE_L - 60 + dx, 420 - k * 40 + dx // 3))
    im.alpha_composite(base)
    d = ImageDraw.Draw(im)
    text(d, (SAFE_L + 30, 520), "主标题", font(F_BOLD, 128), INK + (255,))
    text(d, (SAFE_L + 30, 680), "写在这里", font(F_BOLD, 128), (196, 170, 255, 255))
    text(d, (SAFE_L + 30, 880), "副标题一句话 · 第 00 期", font(F_SEMI, 44), (176, 168, 200, 235))
    st = sts[4].resize((760, int(760 * sts[4].height / sts[4].width)), Image.LANCZOS)
    paste_sticker(im, st, (1520, 430))
    return im


def c06_grid(sts: list[Image.Image]) -> Image.Image:
    im = canvas((10, 12, 16))
    grid(im, 96, (120, 170, 255, 14))
    glow(im, [1500, -200, 2700, 900], (60, 150, 255), blur=200, alpha=70)
    d = ImageDraw.Draw(im)
    for i in range(3):
        d.ellipse([SAFE_L + i * 26, 200 + i * 26, SAFE_L + 26 + i * 26, 226 + i * 26],
                  outline=(90, 160, 255, 200), width=3)
    text(d, (SAFE_L, 300), "主标题写在", font(F_BOLD, 138), INK + (255,))
    text(d, (SAFE_L, 470), "这里两行", font(F_BOLD, 138), (110, 180, 255, 255))
    text(d, (SAFE_L, 680), "副标题：一句话说清这期讲什么", font(F_SEMI, 46), (150, 175, 205, 235))
    d.rectangle([0, H - 210, W, H - 150], fill=(16, 20, 28, 255))
    text(d, (SAFE_L, H - 200), "第 00 期   ·   从零做 AI 应用   ·   zkysever.com", font(F_SEMI, 36), (130, 160, 200, 240))
    st = sts[5].resize((760, int(760 * sts[5].height / sts[5].width)), Image.LANCZOS)
    paste_sticker(im, st, (1560, 380))
    return im


def c07_glitch(sts: list[Image.Image]) -> Image.Image:
    im = canvas((6, 6, 10))
    glow(im, [200, 700, 1400, 1600], (255, 40, 90), blur=170, alpha=60)
    glow(im, [1200, 300, 2400, 1300], (40, 220, 255), blur=200, alpha=60)
    d = ImageDraw.Draw(im)
    f = font(F_BOLD, 150)
    for dx, col in [(-10, (255, 40, 90, 140)), (10, (40, 220, 255, 140)), (0, (255, 255, 255, 255))]:
        d.text((SAFE_L + dx, 380 + (dx // 3)), "主标题写这里", font=f, fill=col)
    d.text((SAFE_L, 580), "副标题：一句话 · 第 00 期", font=font(F_SEMI, 50), fill=(210, 220, 240, 235))
    d.line([(SAFE_L, 720), (SAFE_R - 500, 720)], fill=(255, 255, 255, 70), width=3)
    d.text((SAFE_L, 780), "从零做 AI 应用", font=font(F_MONO, 34), fill=(120, 240, 255, 235))
    scanlines(im, 26)
    # 故障切片
    for y in (300, 520, 700, 900):
        band = im.crop((0, y, W, y + 10))
        im.alpha_composite(band, (18 if y % 200 else -22, y))
    st = sts[6].resize((780, int(780 * sts[6].height / sts[6].width)), Image.LANCZOS)
    halo = Image.new("RGBA", st.size, (60, 220, 255, 255))
    halo.putalpha(st.getchannel("A").filter(ImageFilter.MaxFilter(31)).point(lambda v: int(v * 0.35)))
    im.alpha_composite(halo.filter(ImageFilter.GaussianBlur(34)), (1600, 470))
    paste_sticker(im, st, (1600, 470))
    return im


def c08_dash(sts: list[Image.Image]) -> Image.Image:
    im = canvas((11, 13, 15))
    d = ImageDraw.Draw(im)
    d.rectangle([SAFE_L, 0, SAFE_R, H], outline=(255, 255, 255, 22), width=2)
    text(d, (SAFE_L + 20, 210), "主标题写这里", font(F_BOLD, 118), INK + (255,))
    text(d, (SAFE_L + 20, 350), "副标题：一句话交代", font(F_SEMI, 46), (160, 190, 170, 240))
    rows = [("01", "数据占位", "120"), ("02", "数据占位", "84%"), ("03", "数据占位", "3.2")]
    for i, (no, label, val) in enumerate(rows):
        y = 520 + i * 150
        d.line([(SAFE_L + 20, y), (SAFE_R - 940, y)], fill=(255, 255, 255, 26), width=2)
        d.text((SAFE_L + 20, y + 26), no, font=font(F_MONO, 30), fill=(90, 230, 150, 235))
        d.text((SAFE_L + 110, y + 30), label, font=font(F_SEMI, 40), fill=(180, 195, 185, 240))
        d.text((SAFE_R - 980, y + 20), val, font=font(F_BOLD, 54), fill=(120, 240, 170, 245), anchor="ra")
    d.line([(SAFE_L + 20, 1000), (SAFE_L + 320, 1000)], fill=(90, 230, 150, 210), width=5)
    text(d, (SAFE_L + 20, 1030), "第 00 期 · 从零做 AI 应用", font(F_SEMI, 36), (120, 175, 145, 235))
    # 圆框头像式贴纸
    st = sts[7].resize((640, int(640 * sts[7].height / sts[7].width)), Image.LANCZOS)
    paste_sticker(im, st, (1620, 430))
    return im


def polaroid(st: Image.Image, pad: int = 30, bottom: int = 90, rot: float = 0.0) -> Image.Image:
    """把表情包做成拍立得：白卡 + 底部留白，深色底的贴纸也不显得突兀。"""
    w, h = st.size
    card = Image.new("RGBA", (w + pad * 2, h + pad * 2 + bottom), (250, 250, 247, 255))
    card.alpha_composite(st, (pad, pad))
    if rot:
        card = card.rotate(rot, expand=True, resample=Image.BICUBIC)
    return card


def c09_collage(sts: list[Image.Image]) -> Image.Image:
    im = canvas((240, 235, 226))  # 浅色底，跟其余深色拉开距离
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dl = ImageDraw.Draw(layer)
    dl.polygon([(0, 0), (1500, 0), (1180, H), (0, H)], fill=(28, 30, 38, 255))
    im.alpha_composite(layer)
    d = ImageDraw.Draw(im)
    text(d, (140, 300), "主标题", font(F_BOLD, 152), (245, 246, 250, 255))
    text(d, (140, 480), "写在这里", font(F_BOLD, 152), (255, 214, 92, 255))
    text(d, (140, 690), "副标题一句话 · 第 00 期", font(F_SEMI, 44), (190, 195, 210, 240))
    d.line([(140, 800), (520, 800)], fill=(255, 214, 92, 255), width=8)
    big = sts[8].resize((560, int(560 * sts[8].height / sts[8].width)), Image.LANCZOS)
    paste_sticker(im, big, (1320, 120))
    p1 = polaroid(sts[9].resize((420, int(420 * sts[9].height / sts[9].width)), Image.LANCZOS), rot=-5)
    paste_sticker(im, p1, (1840, 470))
    p2 = polaroid(sts[10].resize((330, int(330 * sts[10].height / sts[10].width)), Image.LANCZOS), rot=7)
    paste_sticker(im, p2, (1420, 900))
    return im


def c10_gradient(sts: list[Image.Image]) -> Image.Image:
    im = vgrad((16, 26, 48), (6, 8, 14))
    glow(im, [900, 200, 2300, 1400], (40, 120, 255), blur=220, alpha=80)
    d = ImageDraw.Draw(im)
    d.text((SAFE_L, 400), "主标题写成两行也可以", font=font(F_BOLD, 120), fill=(240, 244, 250, 255))
    d.text((SAFE_L, 560), "第二行主标题", font=font(F_BOLD, 120), fill=(130, 180, 255, 255))
    d.text((SAFE_L, 760), "副标题：一句话说这期做了什么", font=font(F_SEMI, 46), fill=(160, 180, 210, 240))
    d.rounded_rectangle([SAFE_L, 1130, SAFE_L + 520, 1146], 8, fill=(255, 255, 255, 26))
    d.rounded_rectangle([SAFE_L, 1130, SAFE_L + 190, 1146], 8, fill=(90, 160, 255, 235))
    d.text((SAFE_L, 1180), "第 00 期 · 从零做 AI 应用", font=font(F_SEMI, 34), fill=(140, 165, 200, 235))
    # 大圆角贴纸卡
    st = sts[0].resize((720, int(720 * sts[0].height / sts[0].width)), Image.LANCZOS)
    card = Image.new("RGBA", (860, 860), (255, 255, 255, 0))
    ImageDraw.Draw(card).rounded_rectangle([0, 0, 859, 859], 96, fill=(255, 255, 255, 22),
                                           outline=(255, 255, 255, 46), width=3)
    im.alpha_composite(card, (1470, 330))
    paste_sticker(im, st, (1540, 400))
    return im


LAYOUTS = [
    ("01-终端", c01_terminal),
    ("02-大字报", c02_poster),
    ("03-极简", c03_minimal),
    ("04-电影", c04_cinema),
    ("05-卡片", c05_cards),
    ("06-网格", c06_grid),
    ("07-故障", c07_glitch),
    ("08-数据", c08_dash),
    ("09-拼贴", c09_collage),
    ("10-渐变", c10_gradient),
]


def sheet(paths: list[Path]) -> Path:
    cols, rows = 4, 3
    cw, ch = 640, 360
    out = Image.new("RGB", (cols * cw, rows * ch), (14, 14, 16))
    d = ImageDraw.Draw(out)
    for i, p in enumerate(paths):
        x, y = (i % cols) * cw, (i // cols) * ch
        im = Image.open(p).convert("RGB").resize((cw, ch), Image.LANCZOS)
        out.paste(im, (x, y))
        d.rectangle([x, y, x + cw - 1, y + ch - 1], outline=(255, 255, 255, 40), width=1)
        d.text((x + 14, y + 12), p.stem, font=font(F_SEMI, 22), fill=(255, 255, 255))
    dst = OUT / "_总览.png"
    out.save(dst)
    return dst


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    only = sys.argv[1] if len(sys.argv) > 1 else None
    sts = [sticker(p, 900) for p in pick_stickers(12)]
    if len(sts) < 8:
        raise SystemExit(f"表情包不够（只找到 {len(sts)} 张）")
    paths = []
    for name, fn in LAYOUTS:
        if only and not name.startswith(only.zfill(2)):
            continue
        im = fn(sts).convert("RGB")
        p = OUT / f"{name}.png"
        im.save(p, quality=95)
        print(f"[cover] {p.name}  {im.size[0]}x{im.size[1]}")
        paths.append(p)
    if len(paths) >= 3:
        s = sheet(paths)
        print(f"[cover] 总览 {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
