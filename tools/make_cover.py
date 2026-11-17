"""做一张 B 站投稿封面（16:9，2560x1440）。

**安全区**：B 站投稿页会同时展示 16:9 与 4:3 两个比例，4:3 是中心裁切，
也就是只保留 x ∈ [320, 2240]。所以标题和形象都必须落在这个范围里，
否则 4:3 预览会把形象切掉一半。

用法：python tools/make_cover.py <标题> <副标题> <输出路径>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_video import (  # noqa: E402
    BG, MUTED, TEXT, font, glow, load_avatar_frames, wrap,
)

AVATAR = r"C:\Users\Administrator\Desktop\space\deepseek表情包\04_开心\04_开心_01.png"
ACCENT = (91, 140, 255)

W, H = 2560, 1440
SAFE_L, SAFE_R = 320, 2240     # 4:3 中心裁切后的可见范围
AV_SIZE = 430
AV_X = 1560                    # 右边缘 1990，安全
AV_Y = 830


def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else "我给一个 QQ 群装上了记忆"
    sub = sys.argv[2] if len(sys.argv) > 2 else "从零做 AI 应用 第 01 期"
    out = Path(sys.argv[3] if len(sys.argv) > 3 else "out/ep01_cover.png")

    img = Image.new("RGBA", (W, H), BG + (255,))   # glow() 要 RGBA
    img = glow(img, ACCENT, radius=170)
    d = ImageDraw.Draw(img)

    # 左侧竖条
    d.rectangle([SAFE_L + 20, 430, SAFE_L + 32, 430 + 470], fill=ACCENT)

    f1 = font(132, bold=True)
    y = 450
    for line in wrap(d, title, f1, max_w=1120):
        d.text((SAFE_L + 90, y), line, font=f1, fill=TEXT)
        y += 168

    f2 = font(52)
    d.text((SAFE_L + 92, y + 34), sub, font=f2, fill=MUTED)

    av = load_avatar_frames(AVATAR, AV_SIZE)[0][0]
    img.alpha_composite(av, (AV_X, AV_Y))

    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, "PNG")
    print(f"[封面] {out}  {img.size[0]}x{img.size[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
