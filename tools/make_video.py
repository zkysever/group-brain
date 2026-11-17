"""把一段文字稿 + 大肥鱼形象，渲染成 1080p 视频（本机 ffmpeg，无需联网）。

用法：
    python tools/make_video.py storyboard.json out.mp4 [--still]

storyboard.json 形如：
{
  "voice": "zh-CN-XiaoxiaoNeural",
  "rate": "+8%",
  "avatar": "C:/.../04_开心_01.png",          // 静态形象（可选）
  "avatars": ["C:/.../工作_009.gif", "..."],   // 动态形象（优先，按页轮换）
  "accent": "#5b8cff",
  "slides": [
    {"kind": "cover", "title": "…", "sub": "…", "say": "…"},
    {"kind": "bullets", "title": "…", "bullets": ["…", "…"], "say": "…"}
  ]
}

v3（2026-11-16）：
- 右边改成**会动的 GIF**（多张按页轮换，循环播放 GIF 自己的动画）；
- 画面下方加**白色字幕**（按 `say` 切句，时长按字数分配，带阴影）；
- 文字区让出右侧 620px。

设计取舍（写死在这里，省得以后忘）：
- 每页先合成语音，再用语音时长决定这页多长——先定页长再配音永远对不上。
- 字幕烧进画面（B 站手机端默认不开字幕，烧进去最稳），白字 + 黑阴影，不加底板。
- 形象素材是 160~300px 的表情包/GIF，放大到 560，不追求清晰度，追求「一眼看出是谁」。
- GIF 有自己的循环节奏，不再叠加呼吸缩放，只保留轻微上下浮动。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageSequence

W, H = 1920, 1080
FPS = 30
FONT_BD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_RG = r"C:\Windows\Fonts\msyh.ttc"
BG = (15, 17, 21)
PANEL = (23, 26, 33)
TEXT = (230, 232, 238)
MUTED = (139, 147, 167)
WHITE = (255, 255, 255)

# 右侧「讲解位」
AV_SIZE = 560
AV_X = W - AV_SIZE - 96
AV_Y = 230
AV_RESERVE = 620          # 文字区要主动让出的宽度
AV_NAME = "蓝色大肥鱼"

# 字幕
SUB_SIZE = 54
SUB_Y = 930
SUB_MAXW = 1560

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def hex2rgb(s: str) -> tuple[int, int, int]:
    s = (s or "#5b8cff").lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BD if bold else FONT_RG, size)


def wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """按像素宽度折行（中文没有空格，必须按宽度算）。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if draw.textlength(cur + ch, font=f) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _square(im: Image.Image, size: int) -> Image.Image:
    """裁成正方形并缩放。素材自带透明就用它，白底素材才抠白。

    GIF 表情包通常是**透明底**，早期版本一律「贴白底再抠白」，
    结果把整块白椭圆带进了画面（2026-11-16 修）。
    """
    im = im.convert("RGBA")
    alpha = im.getchannel("A")
    lo, _hi = alpha.getextrema()

    if lo < 240:
        out = im                      # 自带透明：原样用
    else:
        out = im.copy()               # 白/浅底：从四边往里泛洪，把「连成一片的浅色」抠掉
        px = out.load()
        w0, h0 = out.size

        def light(x: int, y: int) -> bool:
            r, g, b, _a = px[x, y]
            return r > 200 and g > 200 and b > 200

        seen = bytearray(w0 * h0)
        stack = []
        for x in range(w0):
            for y in (0, h0 - 1):
                if not seen[y * w0 + x] and light(x, y):
                    seen[y * w0 + x] = 1
                    stack.append((x, y))
        for y in range(h0):
            for x in (0, w0 - 1):
                if not seen[y * w0 + x] and light(x, y):
                    seen[y * w0 + x] = 1
                    stack.append((x, y))
        while stack:
            x, y = stack.pop()
            r, g, b, _a = px[x, y]
            px[x, y] = (r, g, b, 0)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < w0 and 0 <= ny < h0 and not seen[ny * w0 + nx] and light(nx, ny):
                    seen[ny * w0 + nx] = 1
                    stack.append((nx, ny))
        # 剩下的接近纯白（被角色包围的空隙）也一起干掉
        for y in range(h0):
            for x in range(w0):
                r, g, b, a = px[x, y]
                if a and r > 244 and g > 244 and b > 244:
                    px[x, y] = (r, g, b, 0)

    w0, h0 = out.size
    side = min(w0, h0)
    # 先内缩 5%：不少素材画布最外圈有一道浅色描边，泛洪进不去
    inset = int(side * 0.05)
    out = out.crop(((w0 - side) // 2 + inset, (h0 - side) // 2 + inset,
                    (w0 + side) // 2 - inset, (h0 + side) // 2 - inset))
    out = out.resize((size, size), Image.LANCZOS)

    # 圆角：GIF 的图往往画到画布边缘，直角会像贴了张照片
    r = int(size * 0.08)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=255)
    out.putalpha(Image.composite(out.getchannel("A"), Image.new("L", (size, size), 0), mask))
    return out


def load_avatar_frames(path: str, size: int) -> list[tuple[Image.Image, int]]:
    """返回 [(帧, 该帧停留毫秒)]。静态 PNG 只有一帧、停留 0。"""
    im = Image.open(path)
    frames: list[tuple[Image.Image, int]] = []
    if getattr(im, "is_animated", False):
        for i, fr in enumerate(ImageSequence.Iterator(im)):
            if i >= 90:
                break
            ms = int(fr.info.get("duration") or 80)
            frames.append((_square(fr.copy(), size), max(20, min(ms, 1000))))
    else:
        frames.append((_square(im.copy(), size), 0))
    return frames or [(Image.new("RGBA", (size, size), (0, 0, 0, 0)), 0)]


def pick_frame(clip: list[tuple[Image.Image, int]], t: float) -> Image.Image:
    """按 GIF 自带帧时长挑帧，避免 4 帧的 GIF 以 30fps 疯转。"""
    if not clip:
        return Image.new("RGBA", (AV_SIZE, AV_SIZE), (0, 0, 0, 0))
    if len(clip) == 1 or clip[0][1] == 0:
        return clip[0][0]
    total = sum(ms for _, ms in clip) / 1000.0
    x = t % total if total > 0 else 0.0
    acc = 0.0
    for img, ms in clip:
        acc += ms / 1000.0
        if x < acc:
            return img
    return clip[-1][0]


def glow(img: Image.Image, rgb: tuple[int, int, int], radius: int = 60) -> Image.Image:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((-300, img.size[1] - 700, 700, img.size[1] + 300), fill=rgb + (52,))
    layer = layer.filter(ImageFilter.GaussianBlur(radius))
    return Image.alpha_composite(img, layer)


def stage(img: Image.Image, accent: tuple[int, int, int]) -> Image.Image:
    """右侧「讲解台」：几层淡光环 + 落地阴影。"""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = AV_X + AV_SIZE // 2, AV_Y + AV_SIZE // 2
    for k, a in ((330, 26), (250, 22), (180, 18)):
        d.ellipse((cx - k, cy - k, cx + k, cy + k), fill=accent + (a,))
    layer = layer.filter(ImageFilter.GaussianBlur(26))
    out = Image.alpha_composite(img, layer)

    sh = Image.new("RGBA", (AV_SIZE, 90), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((40, 20, AV_SIZE - 40, 88), fill=(0, 0, 0, 120))
    sh = sh.filter(ImageFilter.GaussianBlur(18))
    out.alpha_composite(sh, (AV_X, AV_Y + AV_SIZE - 46))
    return out


def name_tag(img: Image.Image, accent: tuple[int, int, int]) -> Image.Image:
    f = font(40, bold=True)
    d = ImageDraw.Draw(img)
    tw = d.textlength(AV_NAME, font=f)
    x = AV_X + (AV_SIZE - tw) / 2 - 26
    y = AV_Y + AV_SIZE + 18
    d.rounded_rectangle((x, y, x + tw + 52, y + 66), radius=33, fill=PANEL + (235,),
                        outline=accent + (140,), width=2)
    d.text((x + 26, y + 10), AV_NAME, font=f, fill=TEXT)
    return img


def build_slide(spec: dict, accent: tuple[int, int, int]) -> Image.Image:
    """只画背景和文字；形象与字幕在逐帧阶段贴。"""
    img = Image.new("RGBA", (W, H), BG + (255,))
    img = glow(img, accent)
    img = stage(img, accent)
    d = ImageDraw.Draw(img)

    pad_x = 130
    kind = spec.get("kind", "bullets")
    max_w = W - pad_x - AV_RESERVE

    title = spec.get("title", "")
    y = 300
    if title:
        f = font(96, bold=True)
        y = 210 if kind == "cover" else 180
        for line in wrap(d, title, f, max_w):
            d.text((pad_x, y), line, font=f, fill=TEXT)
            y += 128

    sub = spec.get("sub")
    if sub:
        f2 = font(46)
        y2 = y + 10
        for line in wrap(d, sub, f2, max_w):
            d.text((pad_x, y2), line, font=f2, fill=MUTED)
            y2 += 66

    bullets = spec.get("bullets") or []
    if bullets:
        y3 = 470 if not sub else max(470, y + 40)
        fb = font(52)
        for b in bullets:
            d.ellipse((pad_x, y3 + 26, pad_x + 14, y3 + 40), fill=accent)
            for line in wrap(d, b, fb, max_w - 60):
                d.text((pad_x + 42, y3), line, font=fb, fill=TEXT)
                y3 += 72
            y3 += 30

    return name_tag(img, accent)


def subtitle_chunks(text: str, max_chars: int = 16) -> list[str]:
    """把一句话切成字幕行：先按标点断，再按字数硬断。"""
    parts = [p for p in re.split(r"(?<=[，。！？；：、,.!?;:])", text) if p.strip()]
    out: list[str] = []
    for p in parts:
        p = p.strip()
        while len(p) > max_chars:
            cut = p[:max_chars]
            out.append(cut)
            p = p[max_chars:]
        if p:
            out.append(p)
    return out or ([text] if text else [])


def draw_subtitle(img: Image.Image, line: str) -> None:
    """白色字幕 + 黑阴影，居中压在画面下方。"""
    if not line:
        return
    d = ImageDraw.Draw(img)
    f = font(SUB_SIZE, bold=True)
    lines = wrap(d, line, f, SUB_MAXW)
    y = SUB_Y - (len(lines) - 1) * 34
    for ln in lines:
        tw = d.textlength(ln, font=f)
        x = (W - tw) / 2
        d.text((x + 2, y + 4), ln, font=f, fill=(0, 0, 0, 190))
        d.text((x, y), ln, font=f, fill=WHITE)
        y += 68


def schedule(chunks: list[str], dur: float) -> list[tuple[float, float, str]]:
    """按字数把整页时长分给每行字幕，返回 (起, 止, 文本)。"""
    total = sum(max(1, len(c)) for c in chunks) or 1
    t, out = 0.0, []
    for c in chunks:
        share = dur * max(1, len(c)) / total
        out.append((t, t + share, c))
        t += share
    return out


def render_frames(bg: Image.Image, clip: list[tuple[Image.Image, int]],
                  n_frames: int, subs: list[tuple[float, float, str]],
                  tmp: Path, tag: str) -> Path:
    base = bg.convert("RGBA")
    for i in range(n_frames):
        t = i / FPS
        frame = base.copy()
        av = pick_frame(clip, t)
        dy = int(round(8 * math.sin(2 * math.pi * t / 2.2)))
        frame.alpha_composite(av, (AV_X, AV_Y + dy))
        line = next((c for a, b, c in subs if a <= t < b), subs[-1][2] if subs else "")
        draw_subtitle(frame, line)
        frame.convert("RGB").save(tmp / f"{tag}_{i:05d}.png", "PNG")
    return tmp / f"{tag}_%05d.png"


def audio_duration(path: str) -> float:
    p = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True, errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr or "")
    if not m:
        return 3.0
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


async def tts(text: str, path: str, voice: str, rate: str) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(path)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    still = "--still" in sys.argv
    if len(args) < 2:
        print("用法: python tools/make_video.py storyboard.json out.mp4 [--still]")
        return 2
    sb = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    out = Path(args[1]).resolve()

    accent = hex2rgb(sb.get("accent", "#5b8cff"))
    voice = sb.get("voice", "zh-CN-XiaoxiaoNeural")
    rate = sb.get("rate", "+8%")

    paths = sb.get("avatars") or ([sb["avatar"]] if sb.get("avatar") else [])
    paths = [p for p in paths if p and Path(p).exists()]
    if not paths:
        print("[warn] storyboard 里没有可用的形象素材")
    avatar_sets = [load_avatar_frames(p, AV_SIZE) for p in paths]
    print(f"[形象] {len(avatar_sets)} 组，帧数 " + ", ".join(str(len(a)) for a in avatar_sets))

    slides = sb.get("slides") or []
    if not slides:
        print("storyboard 里没有 slides")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="gb_video_"))
    segs = []
    for i, spec in enumerate(slides):
        idx = i + 1
        mp3 = tmp / f"{idx:02d}.mp3"
        mp4 = tmp / f"{idx:02d}.mp4"

        say = spec.get("say") or spec.get("title") or ""
        asyncio.run(tts(say, str(mp3), voice, rate))
        dur = audio_duration(str(mp3)) + 0.55

        bg = build_slide(spec, accent)
        subs = schedule(subtitle_chunks(say), dur)
        av_frames = avatar_sets[i % len(avatar_sets)] if avatar_sets else []
        if not av_frames:
            av_frames = [(Image.new("RGBA", (AV_SIZE, AV_SIZE), (0, 0, 0, 0)), 0)]

        if still:
            png = tmp / f"{idx:02d}.png"
            f0 = bg.convert("RGBA")
            f0.alpha_composite(av_frames[0][0], (AV_X, AV_Y))
            draw_subtitle(f0, subs[0][2] if subs else "")
            f0.convert("RGB").save(png)
            cmd = [FFMPEG, "-y", "-loop", "1", "-i", str(png), "-i", str(mp3)]
        else:
            frames = max(2, int(round(dur * FPS)))
            pattern = render_frames(bg, av_frames, frames, subs, tmp, f"s{idx:02d}")
            cmd = [FFMPEG, "-y", "-framerate", str(FPS), "-i", str(pattern), "-i", str(mp3)]

        cmd += [
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
            "-t", f"{dur:.2f}", "-shortest", str(mp4),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            print(f"[fail] 第 {idx} 页渲染失败:\n{(r.stderr or '')[-1500:]}")
            return 1
        print(f"[ok] 第 {idx} 页 {dur:.1f}s  字幕 {len(subs)} 行  {spec.get('title', '')[:24]}")
        segs.append(mp4)

    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
    cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-r", str(FPS), str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print("[fail] 合并失败:\n" + (r.stderr or "")[-1500:])
        return 1
    print(f"[done] {out}  {out.stat().st_size/1024/1024:.1f} MB")
    print(f"[tmp] {tmp}（可以删）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
