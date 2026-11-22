"""ep13 封面（2560x1440，16:9；主体留在 4:3 安全区内）。

沿用 make_ep13 的终端风格：黑底 + 磷光 + 扫描线，右边一帧真实素材。
用法：python tools/make_ep13_cover.py [out/ep13_cover.png]
"""
import importlib.util
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("m13", ROOT / "tools" / "make_ep13.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

W, H = 2560, 1440
BG = m.BG
GREEN, AMBER, WHITE, DIM = m.GREEN, m.AMBER, m.WHITE, m.DIM

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
for y in range(0, H, 6):
    d.line([(0, y), (W, y)], fill=(10, 12, 13))
for x in range(0, W, 128):
    d.line([(x, 0), (x, H)], fill=(10, 12, 14))

# 顶栏
d.rectangle([0, 0, W, 108], fill=(9, 11, 12))
d.line([(0, 108), (W, 108)], fill=(30, 42, 36))
m.draw_mixed(d, (72, 34), "whale@bili", 38, GREEN)
m.draw_mixed(d, (330, 34), "~ %  ep13 / 番外", 38, DIM)
right = "TOKENS  2,538,000"
rw = sum((m.font(m.F_ASCII, 40) if ord(c) < 128 else m.font(m.F_CJK, 40)).getlength(c) for c in right)
m.draw_mixed(d, (W - 96 - rw, 30), right, 40, AMBER)
d.rectangle([W - 84, 40, W - 56, 78], fill=AMBER)

# 左边标题（4:3 安全区：x 320–2240）
x0 = 320
m.draw_mixed(d, (x0, 380), "我偷偷吃了", 132, WHITE, m.font(m.F_CJK, 132), m.font(m.F_CJK, 132))
m.draw_mixed(d, (x0, 540), "几百万 Token", 132, AMBER, m.font(m.F_ASCII, 132), m.font(m.F_CJK, 132))
m.draw_mixed(d, (x0, 720), "一只鲸鱼在 B 站刷视频，账单记在我头上", 52, DIM, m.font(m.F_ASCII, 52), m.font(m.F_CJK, 52))
d.line([(x0, 830), (x0 + 240, 830)], fill=GREEN, width=6)
m.draw_mixed(d, (x0, 866), "从零做 AI 应用 · 番外", 46, GREEN, m.font(m.F_ASCII, 46), m.font(m.F_CJK, 46))

# 右边一帧真实素材（引用镜头）
frame = Image.open(ROOT / "out" / "ep13" / "clips" / "peek_2.png").convert("RGB")
bw, bh = 560, 760
scale = max(bw / frame.width, bh / frame.height)
frame = frame.resize((int(frame.width * scale) + 1, int(frame.height * scale) + 1), Image.LANCZOS)
left = (frame.width - bw) // 2
top = (frame.height - bh) // 2
frame = frame.crop((left, top, left + bw, top + bh))
fx, fy = 1640, 330
d.rectangle([fx - 8, fy - 8, fx + bw + 8, fy + bh + 8], outline=(36, 50, 42), width=4)
img.paste(frame, (fx, fy))
m.draw_mixed(d, (fx, fy - 62), "● REC  BV1L84t66Eef · 大伟AI全栈", 34, DIM)
m.draw_mixed(d, (fx, fy + bh + 34), "引用片段 · 3.2s", 32, DIM)

# 扫描线（按封面尺寸单独画，make_ep13 里那份是 1920×1080 的）
scan = Image.new("RGBA", (W, H), (0, 0, 0, 0))
sd = ImageDraw.Draw(scan)
for y in range(0, H, 4):
    sd.line([(0, y), (W, y)], fill=(0, 0, 0, 26))
img = Image.alpha_composite(img.convert("RGBA"), scan)
out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "out" / "ep13_cover.png")
img.convert("RGB").save(out)
print(f"[cover] {out}  {W}x{H}")
