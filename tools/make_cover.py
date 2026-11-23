"""做一张 B 站投稿封面（2560×1440），**第 04 版「电影海报」**。

用户 2026-11-23 定的版式：深黑底 + 上下黑边 + 左侧大标题 + 右侧竖排系列名 +
右下角表情包（原酱本体，白描边）。十版里挑的第 4 版，已发布过的也统一换成这个。

安全区：B 站同时展示 16:9 与 4:3，4:3 是中心裁切 → 只保留 x ∈ [320, 2240]。

用法：
    python tools/make_cover.py <标题> <副标题> <输出路径>
    python tools/make_cover.py --batch          # 按 out/queue/*/meta.json 全出一遍
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cover_lab import (  # noqa: E402
    SAFE_L,
    SAFE_R,
    W,
    H,
    cut_white,
    drop_shadow,
    font,
    glow,
)

INHERIT = Path(__file__).resolve().parent.parent

BG = (7, 7, 9)
INK = (240, 243, 248)
ACCENT = (150, 190, 255)
DIM = (168, 182, 208)
FAINT = (118, 132, 158)

F_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
F_SEMI = r"C:\Windows\Fonts\msyh.ttc"

BAR = 130                 # 上下黑边
TITLE_SIZE = 150
TITLE_W = 1150            # 标题最大宽度（到 320+1150=1470，离贴纸还有余量）
TITLE_Y = 330
TITLE_STEP = 190
ST_SIZE = 700
ST_X, ST_Y = 1470, 585

HERO = Path(r"C:\Users\Administrator\Desktop\space\deepseek表情包\01_开心\开心_001.png")


def wrap(text: str, f, max_w: int) -> list[str]:
    """按宽度折行（中文没空格，只能量宽度）。"""
    from PIL import ImageFont  # noqa: F401  (类型提示用)

    lines: list[str] = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        trial = cur + ch
        if f.getlength(trial) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def hero_sticker(size: int) -> Image.Image:
    im = cut_white(Image.open(HERO))
    im.thumbnail((size, size), Image.LANCZOS)
    return im


def make(title: str, sub: str, out: Path, sticker: Image.Image | None = None) -> Path:
    st = sticker or hero_sticker(ST_SIZE)
    img = Image.new("RGBA", (W, H), BG + (255,))
    glow(img, [700, 260, 1980, 1240], (62, 96, 255), blur=200, alpha=72)

    d = ImageDraw.Draw(img)
    # 上下黑边（电影感）
    d.rectangle([0, 0, W, BAR], fill=(0, 0, 0, 255))
    d.rectangle([0, H - BAR, W, H], fill=(0, 0, 0, 255))

    f_title = font(F_BOLD, TITLE_SIZE)
    y = TITLE_Y
    for line in wrap(title, f_title, TITLE_W):
        d.text((SAFE_L, y), line, font=f_title, fill=INK + (255,))
        y += TITLE_STEP

    f_sub = font(F_SEMI, 50)
    d.text((SAFE_L, y + 30), sub, font=f_sub, fill=DIM + (255,))

    # 右侧竖排系列名
    f_v = font(F_SEMI, 44)
    vy = 340
    for ch in "从零做 AI 应用":
        if ch.strip():
            d.text((SAFE_R - 58, vy), ch, font=f_v, fill=(120, 140, 178, 225), anchor="ma")
        vy += int(44 * 1.06)

    # 表情包
    sh = drop_shadow(st, blur=22, alpha=130)
    img.alpha_composite(sh, (ST_X + 16, ST_Y + 26))
    img.alpha_composite(st, (ST_X, ST_Y))

    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, "PNG")
    print(f"[封面] {out}  {W}x{H}")
    return out


def batch() -> int:
    """按队列里每期的 meta 出一遍，写 out/covers_new/<id>_cover.png。"""
    dst_dir = INHERIT / "out" / "covers_new"
    st = hero_sticker(ST_SIZE)
    metas = sorted((INHERIT / "out" / "queue").glob("*/meta.json"))
    if not metas:
        raise SystemExit("out/queue 下没有 meta.json")
    for p in metas:
        meta = json.loads(p.read_text(encoding="utf-8-sig"))
        title = meta.get("cover_title") or meta.get("title") or p.parent.name
        sub = meta.get("cover_sub") or "从零做 AI 应用"
        make(title, sub, dst_dir / f"{p.parent.name}_cover.png", st)
    print(f"[封面] 共 {len(metas)} 张 → {dst_dir}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--batch":
        return batch()
    title = sys.argv[1] if len(sys.argv) > 1 else "我给一个 QQ 群装上了记忆"
    sub = sys.argv[2] if len(sys.argv) > 2 else "从零做 AI 应用 · 第 01 期"
    out = Path(sys.argv[3] if len(sys.argv) > 3 else "out/ep01_cover.png")
    make(title, sub, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
