"""每天到点跑一次：找到「今天该发」的那一期，做出来、传上去、记下 BV 号。

为什么长这样：
- 视频生成必须在本机（服务器没有 ffmpeg，2C2G 也跑不动 x264），发布必须借 bsk 的浏览器；
  所以这个脚本是「本机的调度器」，跑在 Windows 任务计划里，不是服务器 cron。
- 生成完先把 mp4/封面 scp 到服务器 /out/，再让浏览器去下载——因为 bsk 只能下载公网直链，
  不能直接读本机路径（`bsk upload` 的 selector 会匹配到 3 个 input）。
- 幂等：state.json 里记了已发布的 id，重复跑不会重复发。

用法：
    python scripts/publish_next.py --dry      # 彩排：做出视频、填好表，不点投稿
    python scripts/publish_next.py            # 真发
    python scripts/publish_next.py --force ep03   # 忽略日期，强制发某一期
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
QUEUE = OUT / "queue"
STATE = QUEUE / "state.json"
SECRET = Path(os.environ["USERPROFILE"]) / ".dsh" / "secrets" / "bili-upload-cookie.txt"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
SERVER = "root@118.178.109.54"
SSH_KEY = Path(os.environ["USERPROFILE"]) / ".ssh" / "zkysever.pem"


def log(msg: str) -> None:
    print(f"[next] {msg}", flush=True)


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"published": {}, "last_run": ""}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_episode(force: str | None) -> dict | None:
    idx = json.loads((QUEUE / "index.json").read_text(encoding="utf-8"))
    st = load_state()
    eps = idx["episodes"]
    if force:
        for e in eps:
            if e["id"] == force:
                return e
        raise SystemExit(f"index.json 里没有 {force}")
    today = date.today().isoformat()
    for e in eps:
        if e["id"] in st["published"]:
            continue
        if e["date"] <= today:
            return e
        return None   # 排过序，后面的都还没到
    return None


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], cwd=ROOT, text=True, encoding="utf-8",
                          errors="replace", **kw)


def build(ep: dict) -> tuple[Path, Path, Path]:
    """返回 (video, cover, meta)。已存在就跳过生成。"""
    d = QUEUE / ep["id"]
    meta_path = d / "meta.json"
    slides = d / "slides.json"
    if not meta_path.exists() or not slides.exists():
        raise SystemExit(f"{ep['id']} 缺 meta.json 或 slides.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    video = OUT / f"{ep['id']}.mp4"
    cover = OUT / f"{ep['id']}_cover.png"
    if not video.exists():
        r = run([PY, "tools/make_video.py", slides, video])
        if r.returncode != 0:
            raise SystemExit("视频渲染失败")
    if not cover.exists():
        r = run([PY, "tools/make_cover.py", meta.get("cover_title", meta["title"]),
                 meta.get("cover_sub", ""), cover])
        if r.returncode != 0:
            raise SystemExit("封面渲染失败")
    return video, cover, meta_path


def push_to_server(paths: list[Path]) -> None:
    for p in paths:
        r = run(["scp", "-q", "-P", "2222", "-i", SSH_KEY, p, f"{SERVER}:/opt/group-brain/out/"])
        if r.returncode != 0:
            raise SystemExit(f"scp 失败: {p.name}")


def latest_bv(cookie: str, before: set[str]) -> str | None:
    url = ("https://member.bilibili.com/x/web/archives"
           "?status=is_pubing,pubed,not_pubed&pn=1&ps=5")
    req = urllib.request.Request(url, headers={
        "Cookie": cookie, "User-Agent": "Mozilla/5.0",
        "Referer": "https://member.bilibili.com/platform/upload-manager/article",
        "X-Requested-With": "XMLHttpRequest",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        js = json.loads(r.read().decode("utf-8"))
    for a in (js.get("data") or {}).get("arc_audits") or []:
        arc = a.get("Archive") or {}
        bv = arc.get("bvid")
        if bv and bv not in before:
            return bv
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", default=None)
    ap.add_argument("--session", default=os.environ.get("BSK_SESSION", ""),
                    help="bsk 会话 id（用户扫完码的那个）")
    ap.add_argument("--skip-login", action="store_true",
                    help="已经登录过了，跳过等扫码")
    args = ap.parse_args()

    ep = pick_episode(args.force)
    if not ep:
        log("今天没有该发的，退出")
        return 0
    log(f"今天发 {ep['id']}（计划 {ep['date']}）")

    video, cover, meta_path = build(ep)
    log(f"  视频 {video.name} {video.stat().st_size // 1024} KB / 封面 {cover.name}")
    push_to_server([video, cover])

    cookie = SECRET.read_text(encoding="utf-8").strip() if SECRET.exists() else ""
    if not cookie:
        log("  没有 upload 账号 cookie：发布能跑，但查不到 BV 号")

    st = load_state()
    before = set(st["published"].values()) if st["published"] else set()

    token = os.environ.get("OUT_TOKEN", "")
    if not token:
        raise SystemExit("缺 OUT_TOKEN 环境变量")
    video_url = f"https://zkysever.com/out/{video.name}?k={token}"
    cover_url = f"https://zkysever.com/out/{cover.name}?k={token}"

    cmd = [PY, "tools/publish_bsk.py", "--meta", meta_path, "--video-url", video_url,
           "--cover-url", cover_url]
    # 顺手补历史稿件的简介：out/queue/desc_fix.json 里写 {"aid": "...", "file": "out/queue/ep01/desc.txt"}
    fix_path = ROOT / "out" / "queue" / "desc_fix.json"
    if fix_path.exists():
        try:
            for item in json.loads(fix_path.read_text(encoding="utf-8")):
                aid, f = str(item.get("aid") or "").strip(), str(item.get("file") or "").strip()
                if aid and f:
                    cmd += ["--fix-desc", f"{aid}={f}"]
                    log(f"  发布后会顺手补简介：aid={aid}")
        except (ValueError, OSError) as exc:
            log(f"  desc_fix.json 读不了，跳过：{exc}")
    if args.session:
        cmd += ["--session", args.session]
    if args.skip_login:
        cmd.append("--skip-login")
    if args.dry:
        cmd.append("--dry")
    r = run(cmd)
    if r.returncode != 0:
        raise SystemExit("发布脚本失败（看上面的 bsk 报错）")

    if args.dry:
        log("dry-run 完成，没有提交")
        return 0

    time.sleep(8)
    bv = None
    if cookie:
        for _ in range(6):
            try:
                bv = latest_bv(cookie, before)
            except Exception as e:  # 网络抖一下不算失败
                log(f"  查 BV 失败，重试: {e}")
            if bv:
                break
            time.sleep(10)
    if bv:
        log(f"  已发布 {bv}")
        st["published"][ep["id"]] = bv
    else:
        log("  没查到 BV：可能还在审核，稍后手动补记")
        st["published"][ep["id"]] = "pending"
    st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
