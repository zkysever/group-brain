"""第 13 期·番外《我偷偷吃了几百万 Token》——终端录像风格。

和 ep01–ep12 的幻灯片完全不同的做法：没有大标题排版、没有右侧 GIF 形象，
换成一块绿色磷光的终端（打字机 + 扫描线 + 计数器），右边一个播放器窗口放真实引用片段。

素材与数字全部来自真数据：out/ep13/data.json（B 站 view/player 接口）与
out/ep13/clips/*.mp4（用 playurl 截的 3 段 3.2 秒）。

用法：python tools/make_ep13.py [out/ep13.mp4]
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FPS = 30
ROOT = Path(__file__).resolve().parent.parent
EP13 = ROOT / "out" / "ep13"

F_ASCII = r"C:\Windows\Fonts\consolab.ttf"
F_CJK = r"C:\Windows\Fonts\simsun.ttc"
F_SUB = r"C:\Windows\Fonts\msyhbd.ttc"

BG = (7, 8, 10)
PANEL = (12, 15, 14)
GREEN = (124, 255, 178)
GREEN_DIM = (74, 156, 108)
AMBER = (255, 196, 107)
TEXT = (214, 222, 216)
DIM = (126, 140, 132)
WHITE = (255, 255, 255)

VOICE = "zh-CN-YunxiNeural"
RATE = "+6%"

TERM_X, TERM_Y, TERM_W = 96, 210, 1080
LINE_H = 62
TERM_SIZE = 38
PLAY_X, PLAY_Y, PLAY_W, PLAY_H = 1216, 196, 620, 430
SUB_Y, SUB_SIZE = 946, 44


# ---------------------------------------------------------------- 工具

def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def draw_mixed(d: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, size: int, fill,
               ascii_font: ImageFont.FreeTypeFont | None = None,
               cjk_font: ImageFont.FreeTypeFont | None = None) -> int:
    """逐字画：ASCII 用 Consolas（等宽），中文用 SimSun。返回结束 x。"""
    af = ascii_font or font(F_ASCII, size)
    cf = cjk_font or font(F_CJK, size)
    x, y = xy
    for ch in text:
        f = af if ord(ch) < 128 else cf
        d.text((x, y), ch, font=f, fill=fill)
        x += f.getlength(ch)
    return int(x)


def _prefer_ipv4() -> None:
    """只走 IPv4（这台机器 IPv6 出口是坏的，edge-tts 会 WinError 10054）。"""
    if getattr(_prefer_ipv4, "_done", False):
        return
    orig = socket.getaddrinfo

    def patched(host, port, family=0, type=0, proto=0, flags=0):
        infos = orig(host, port, family, type, proto, flags)
        v4 = [i for i in infos if i[0] == socket.AF_INET]
        return v4 or infos

    socket.getaddrinfo = patched
    _prefer_ipv4._done = True


async def tts(text: str, path: str) -> None:
    import edge_tts

    _prefer_ipv4()
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(path)


def audio_duration(path: str) -> float:
    p = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True, errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr or "")
    if not m:
        return 4.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def extract_frames(src: Path, dst: Path, box: tuple[int, int], fps: int = 12) -> list[Path]:
    """把 3 秒素材抽成小帧（等比缩放后居中裁切到 box），用于播放器窗口。"""
    dst.mkdir(parents=True, exist_ok=True)
    bw, bh = box
    vf = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase,"
          f"crop={bw}:{bh},fps={fps}")
    cmd = [FFMPEG, "-y", "-i", str(src), "-vf", vf, str(dst / "f_%03d.png")]
    subprocess.run(cmd, check=True, capture_output=True)
    return sorted(dst.glob("f_*.png"))


import imageio_ffmpeg  # noqa: E402  (放在函数后，避免上面 import 顺序争议)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


# ---------------------------------------------------------------- 数据

def load_numbers() -> dict:
    """把真实数据换算成屏幕上那几个数字，口径写在 notes 里。"""
    data = json.loads((EP13 / "data.json").read_text(encoding="utf-8"))
    first = data[0]          # 34 分钟、12.9 万弹幕、无字幕
    with_sub = next((d for d in data if d.get("sub_chars")), data[1])

    frames, frame_tok = 12, 1100
    dm_n, dm_chars = 600, 14
    cm_n, cm_chars = 100, 45
    sub_chars = with_sub["sub_chars"]

    cost = {
        "抽帧": frames * frame_tok,
        "弹幕": int(dm_n * dm_chars * 0.8),
        "热评": int(cm_n * cm_chars * 0.8),
        "字幕": int(sub_chars * 0.8),
        "prompt": 2400,
    }
    per = sum(cost.values())
    round_per = int(round(per / 100) * 100)
    return {
        "first": first,
        "with_sub": with_sub,
        "frames": frames,
        "dm_n": dm_n,
        "cm_n": cm_n,
        "sub_chars": sub_chars,
        "cost": cost,
        "per": round_per,
        "day": round_per * 3,
        "month": round_per * 3 * 30,
    }


def mmss(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def thousands(n: int) -> str:
    return f"{n:,}"


# ---------------------------------------------------------------- 分镜

def build_beats(n: dict) -> list[dict]:
    f = n["first"]
    per, day, month = n["per"], n["day"], n["month"]
    c = n["cost"]
    return [
        {
            "say": "我偷偷吃了几百万 Token。一只鲸鱼，住在我的电脑里，每天拿我的钱，去 B 站刷视频。",
            "lines": [
                ("prompt", "$ whoami"),
                ("out", "原酱 · 一只住在 B 站的鲸鱼"),
                ("prompt", "$ echo $BILL_TO"),
                ("hl", "主人"),
            ],
            "tok": 0,
            "used": 1,
        },
        {
            "say": "她一天最多完整看三条。但看之前，先把候选池过一遍——两百多条，挑出四条。",
            "lines": [
                ("prompt", "$ node tools/bili.mjs feed"),
                ("out", "候选池 267 条  →  挑 4 条"),
                ("out", "用时 3.4s"),
                ("dim", "# 12 词 × 2 排序 × 2 页"),
            ],
            "tok": 900,
            "clip": 1,
        },
        {
            "say": "第一条，三十四分钟。弹幕十二万九千条，评论八万九。没有字幕。",
            "lines": [
                ("prompt", f"$ node tools/bili.mjs debrief {f['bvid']}"),
                ("out", f"时长 {mmss(f['duration'])}   弹幕 {thousands(f['danmaku'])}   评论 {thousands(f['reply'])}"),
                ("hl", "字幕 0 行   ← 没有现成的"),
            ],
            "tok": 3200,
            "clip": 0,
        },
        {
            "say": "所以她要六路看懂：封面、简介、字幕、弹幕热区、热评，还有抽帧。每一路，都是一次模型调用。",
            "lines": [
                ("out", "6 路理解"),
                ("out", " 1 封面     2 简介     3 字幕"),
                ("out", " 4 弹幕热区  5 热评     6 抽帧"),
                ("hl", "每一路 = 1 次调用"),
                ("dim", "# 6 路之后还要归纳成一段记忆"),
            ],
            "tok": 13000,
            "clip": 2,
        },
        {
            "say": "我替她算过账。一条三十四分钟的视频，大概两万八千个 Token。",
            "lines": [
                ("out", f"token 估算 / 条   （{mmss(f['duration'])} 那条）"),
                ("out", f"抽帧 {n['frames']}×1,100        {thousands(c['抽帧'])}"),
                ("out", f"弹幕 {n['dm_n']}×14字         {thousands(c['弹幕'])}"),
                ("out", f"热评 {n['cm_n']}×45字         {thousands(c['热评'])}"),
                ("out", f"字幕 {thousands(n['sub_chars'])} 字       {thousands(c['字幕'])}"),
                ("out", f"prompt + 归纳        {thousands(c['prompt'])}"),
                ("hl", f"≈ {thousands(per)}"),
                ("dim", "# 口径：字数×0.8，帧数×1,100，不是发票"),
            ],
            "used": 2,
            "tok": per,
        },
        {
            "say": "一天三条，八万四。一个月，两百五十二万。几百万，是这么吃出来的。",
            "lines": [
                ("out", f"3 条/天  ×  {thousands(per)}  =  {thousands(day)}"),
                ("out", f"30 天                =  {thousands(month)}"),
                ("hl", "「几百万」"),
            ],
            "tok": day,
            "big": (thousands(month), "一个月"),
        },
        {
            "say": "所以我给她装了一个闸门。今天三条看满，第四条，直接拒绝。",
            "lines": [
                ("prompt", "$ node tools/bili.mjs deep"),
                ("out", '{"used":3,"limit":3,"left":0}'),
                ("prompt", f"$ node tools/bili.mjs debrief {f['bvid']}"),
                ("hl", "[deep-budget] 今天已经完整看了 3/3 条"),
                ("dim", "# 在下载之前就拦住，不是提醒，是拒绝"),
            ],
            "used": 3,
            "tok": day,
        },
        {
            "say": "她不知道自己在吃钱。她只知道，今天还有三条可以看。",
            "lines": [
                ("prompt", "$ echo 她不知道的是"),
                ("hl", "账单 → 主人"),
                ("prompt", "$ echo 她知道的是"),
                ("out", "今天还有三条可以看"),
            ],
            "tok": day,
            "big": (thousands(day), "今天"),
        },
        {
            "say": "下一期，一个人怎么养住它。",
            "lines": [
                ("dim", "# 番外 · 完"),
                ("out", "下一期：一个人怎么养住它"),
                ("prompt", "$ _"),
            ],
            "nomark": True,
            "tok": day,
        },
    ]


# ---------------------------------------------------------------- 画面

def base_layer() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for y in range(0, H, 4):                     # 细网格
        d.line([(0, y), (W, y)], fill=(11, 13, 14))
    for x in range(0, W, 96):
        d.line([(x, 0), (x, H)], fill=(11, 13, 15))
    # 顶栏
    d.rectangle([0, 0, W, 78], fill=(10, 12, 13))
    d.line([(0, 78), (W, 78)], fill=(30, 42, 36))
    # 播放器外框
    d.rectangle([PLAY_X - 2, PLAY_Y - 2, PLAY_X + PLAY_W + 2, PLAY_Y + PLAY_H + 2], outline=(34, 48, 41))
    return img


def scanlines() -> Image.Image:
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for y in range(0, H, 3):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, 26))
    return ov


def vignette() -> Image.Image:
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for i in range(90):
        a = int(70 * (1 - i / 90))
        d.rectangle([i * 3, i * 2, W - i * 3, H - i * 2], outline=(0, 0, 0, a))
    return ov


class Renderer:
    def __init__(self, n: dict, clip_frames: dict[int, list[Image.Image]]):
        self.n = n
        self.clip_frames = clip_frames
        self.base = base_layer()
        self.scan = scanlines()
        self.vig = vignette()
        self.f_ascii = font(F_ASCII, 30)
        self.f_cjk = font(F_CJK, 30)
        self.f_ascii_s = font(F_ASCII, 24)
        self.f_cjk_s = font(F_CJK, 24)
        self.f_big = font(F_ASCII, 128)
        self.f_sub = font(F_SUB, SUB_SIZE)
        self._cache: dict[tuple, Image.Image] = {}

    def line_img(self, kind: str, text: str, small: bool = False) -> Image.Image:
        key = (kind, text, small)
        if key in self._cache:
            return self._cache[key]
        color = {"prompt": GREEN, "out": TEXT, "hl": AMBER, "dim": DIM}[kind]
        size = TERM_SIZE - 8 if small else TERM_SIZE
        af = font(F_ASCII, size)
        cf = font(F_CJK, size)
        tmp = Image.new("RGBA", (TERM_W + 200, size + 24), (0, 0, 0, 0))
        d = ImageDraw.Draw(tmp)
        draw_mixed(d, (0, 0), text, size, color, af, cf)
        self._cache[key] = tmp
        return tmp

    def gauge(self, d: ImageDraw.ImageDraw, t: float, used: int, total: int = 3) -> None:
        """“今日完整观看”水表：三个格子，看完一个点亮一个。"""
        draw_mixed(d, (PLAY_X + 24, PLAY_Y + 8), "今日完整观看", 26, DIM, self.f_ascii, self.f_cjk)
        draw_mixed(d, (PLAY_X + 24, PLAY_Y + 46), f"{used} / {total}", 40, AMBER, font(F_ASCII, 40), font(F_CJK, 40))
        y = PLAY_Y + 130
        for i in range(total):
            on = i < used
            box_h = 74
            d.rectangle([PLAY_X + 24, y, PLAY_X + 24 + 190, y + box_h],
                        outline=(40, 92, 66) if on else (32, 44, 38), width=2,
                        fill=(14, 34, 24) if on else None)
            if on:
                # 进度感：正在“读”的那一格有扫光
                fill_w = int(190 * (0.35 + 0.6 * min(1.0, t / 6)))
                d.rectangle([PLAY_X + 26, y + 2, PLAY_X + 24 + fill_w, y + box_h - 2], fill=(20, 58, 40))
                draw_mixed(d, (PLAY_X + 40, y + 20), f"看完 {i + 1}", 24, GREEN, self.f_ascii_s, self.f_cjk_s)
            else:
                draw_mixed(d, (PLAY_X + 40, y + 20), "额度还在", 24, (58, 74, 64), self.f_ascii_s, self.f_cjk_s)
            y += box_h + 14
        draw_mixed(d, (PLAY_X + 24, y + 6), "超了就 exit 3，不给看", 22, DIM, self.f_ascii_s, self.f_cjk_s)

    def frame(self, beat: dict, t: float, dur: float, prev_tok: int) -> Image.Image:
        img = self.base.copy()
        d = ImageDraw.Draw(img)

        # 顶栏
        draw_mixed(d, (48, 26), "whale@bili", 26, GREEN, self.f_ascii, self.f_cjk)
        draw_mixed(d, (214, 26), "~ %  ep13 / 番外", 26, DIM, self.f_ascii, self.f_cjk)
        prog = min(1.0, t / max(0.001, dur * 0.8))
        tok = prev_tok + (beat["tok"] - prev_tok) * prog
        right = f"TOKENS  {thousands(int(max(0, tok)))}"
        w = sum((self.f_ascii if ord(ch) < 128 else self.f_cjk).getlength(ch) for ch in right)
        draw_mixed(d, (W - 60 - w, 24), right, 28, AMBER, self.f_ascii, self.f_cjk)
        if int(t * 2) % 2 == 0:
            d.rectangle([W - 52, 30, W - 34, 58], fill=AMBER)

        # 终端正文：逐行显示 + 最后一行打字机
        lines = beat["lines"]
        n_lines = len(lines)
        typed_done = 0
        for i, (kind, text) in enumerate(lines):
            appear = dur * (0.04 + 0.72 * (i / max(1, n_lines)))
            if t < appear:
                break
            shown = text
            typing = (t - appear) < 0.42
            if typing:
                frac = min(1.0, (t - appear) / 0.42)
                shown = text[: max(0, int(len(text) * frac))]
            im = self.line_img(kind, shown)
            img.paste(im, (TERM_X, TERM_Y + i * LINE_H), im)
            if shown:
                typed_done = i
        # 光标停在最后一个可见行末尾
        if typed_done < n_lines and (int(t * 3) % 2 == 0):
            (kind, text) = lines[typed_done]
            cur = self.line_img(kind, text)
            x_end = TERM_X + cur.getbbox()[2] if cur.getbbox() else TERM_X
            d.rectangle([x_end + 4, TERM_Y + typed_done * LINE_H + 6,
                         x_end + 20, TERM_Y + typed_done * LINE_H + 38], fill=GREEN)

        # 播放器 / 大数字
        if beat.get("clip") is not None:
            frames = self.clip_frames.get(beat["clip"]) or []
            if frames:
                loop = frames + frames[::-1]
                idx = int(t * 12) % len(loop)
                img.paste(loop[idx], (PLAY_X, PLAY_Y))
                info = ["BV1xx411c7mD · 碧诗", "BV1oVnq6DE41 · 生肉烹饪师", "BV1L84t66Eef · 大伟AI全栈"]
                draw_mixed(d, (PLAY_X, PLAY_Y - 34), "● REC  " + info[beat["clip"] % 3], 22, DIM,
                           self.f_ascii_s, self.f_cjk_s)
                draw_mixed(d, (PLAY_X, PLAY_Y + PLAY_H + 14), "引用片段 · 3.2s", 20, DIM,
                           self.f_ascii_s, self.f_cjk_s)
                bw = int(PLAY_W * min(1.0, t / max(0.001, dur)))
                d.rectangle([PLAY_X, PLAY_Y + PLAY_H + 44, PLAY_X + bw, PLAY_Y + PLAY_H + 48], fill=GREEN_DIM)
        elif beat.get("nomark"):
            # 收尾卡：把播放器框抹掉，留一块干净的黑
            d.rectangle([PLAY_X - 4, PLAY_Y - 4, PLAY_X + PLAY_W + 4, PLAY_Y + PLAY_H + 4], fill=BG)
        elif beat.get("big"):
            num, label = beat["big"]
            draw_mixed(d, (PLAY_X + 30, PLAY_Y + 120), label, 28, DIM, self.f_ascii, self.f_cjk)
            draw_mixed(d, (PLAY_X + 30, PLAY_Y + 168), num, 96, AMBER, font(F_ASCII, 96), font(F_CJK, 96))
            draw_mixed(d, (PLAY_X + 30, PLAY_Y + 300), "tokens（估算）", 22, DIM,
                       self.f_ascii_s, self.f_cjk_s)
        else:
            self.gauge(d, t, int(beat.get("used", 0)))

        # 字幕
        say = beat["say"]
        chunks = self._sub_chunks(say)
        sched = self._schedule(chunks, dur)
        cur = next((c for (a, b, c) in sched if a <= t <= b), sched[-1][2] if sched else "")
        if cur:
            tw = self.f_sub.getlength(cur)
            x0 = int((W - tw) / 2)
            d.rectangle([x0 - 20, SUB_Y - 12, x0 + tw + 20, SUB_Y + SUB_SIZE + 14], fill=(0, 0, 0))
            d.text((x0, SUB_Y), cur, font=self.f_sub, fill=WHITE)
            d.rectangle([x0 - 26, SUB_Y - 6, x0 - 18, SUB_Y + SUB_SIZE + 8], fill=GREEN)

        # 扫描线 + 暗角 + 轻微闪烁
        img = Image.alpha_composite(img.convert("RGBA"), self.scan)
        img = Image.alpha_composite(img, self.vig)
        out = img.convert("RGB")
        if random.random() < 0.08:
            fade = Image.new("RGB", (W, H), (255, 255, 255))
            out = Image.blend(out, fade, 0.02)
        return out

    @staticmethod
    def _sub_chunks(text: str, max_chars: int = 18) -> list[str]:
        parts = re.split(r"(?<=[，。！？、；：])", text)
        out: list[str] = []
        for p in parts:
            p = p.strip()
            while len(p) > max_chars:
                out.append(p[:max_chars])
                p = p[max_chars:]
            if p:
                out.append(p)
        return out

    @staticmethod
    def _schedule(chunks: list[str], dur: float) -> list[tuple[float, float, str]]:
        if not chunks:
            return []
        total = sum(len(c) for c in chunks) or 1
        t, out = 0.0, []
        for c in chunks:
            d = dur * len(c) / total
            out.append((t, t + d, c))
            t += d
        return out


# ---------------------------------------------------------------- 主流程

def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else EP13 / "ep13.mp4").resolve()
    n = load_numbers()
    beats = build_beats(n)
    print(f"[数字] 单条 ≈ {thousands(n['per'])} / 当天 {thousands(n['day'])} / 一月 {thousands(n['month'])}")

    tmp = Path(tempfile.mkdtemp(prefix="ep13_"))
    # 素材帧
    clip_frames: dict[int, list[Image.Image]] = {}
    for i, f in enumerate(sorted((EP13 / "clips").glob("clip_*.mp4"))):
        idx = int(re.search(r"clip_(\d)", f.name).group(1))
        d = tmp / f"cf{idx}"
        frames = extract_frames(f, d, (PLAY_W, PLAY_H))
        clip_frames[idx] = [Image.open(p).convert("RGB") for p in frames]
        print(f"[素材] clip_{idx} {len(frames)} 帧")

    r = Renderer(n, clip_frames)
    segs = []
    prev_tok = 0
    for i, beat in enumerate(beats, 1):
        mp3 = tmp / f"{i:02d}.mp3"
        mp4 = tmp / f"{i:02d}.mp4"
        asyncio.run(tts(beat["say"], str(mp3)))
        dur = audio_duration(str(mp3)) + 0.5
        total = max(6, int(round(dur * FPS)))
        fdir = tmp / f"f{i:02d}"
        fdir.mkdir(exist_ok=True)
        for k in range(total):
            t = k / FPS
            r.frame(beat, t, dur, prev_tok).save(fdir / f"{k:04d}.png")
        cmd = [FFMPEG, "-y", "-framerate", str(FPS), "-i", str(fdir / "%04d.png"),
               "-i", str(mp3), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-pix_fmt", "yuv420p", "-r", str(FPS),
               "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
               "-t", f"{dur:.2f}", "-shortest", str(mp4)]
        rc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if rc.returncode != 0:
            print(f"[fail] 第 {i} 段:\n{(rc.stderr or '')[-1200:]}")
            return 1
        print(f"[ok] 第 {i} 段 {dur:.1f}s  {beat['say'][:18]}")
        segs.append(mp4)
        prev_tok = beat["tok"]

    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8")
    cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
           "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-r", str(FPS), str(out)]
    rc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if rc.returncode != 0:
        print("[fail] 合并:\n" + (rc.stderr or "")[-1200:])
        return 1
    print(f"[done] {out}  {out.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
