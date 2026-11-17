"""每天 13:00 的「备片 + 发到 QQ」：只做三件事，不发布。

1. 挑出今天该发的那一期（逻辑复用 publish_next.pick_episode）；
2. 渲染视频 + 封面，推一份到服务器 `/out/`（公网可看，手机点开就能看）；
3. 写一条投递单，让 QQ 插件把视频直接发给你（视频段只认 URL：
   两个容器不共享路径，传本地路径会 ENOENT）。

发布不在这里做——等你在 QQ 回一句「可以」，再跑 scripts/publish_next.py。

用法：
    python scripts/prep_next.py            # 今天该发什么就备什么
    python scripts/prep_next.py --force ep02
    python scripts/prep_next.py --dry      # 只挑，不渲染不发
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from publish_next import OUT, ROOT, build, log, pick_episode, push_to_server, run

OUTBOX = "/opt/qqbot/astrbot-data/curator/outbox"
SSH_KEY = Path.home() / ".ssh" / "zkysever.pem"
OUT_TOKEN = os.environ.get("OUT_TOKEN", "")


def public_url(video: Path) -> str:
    """给 QQ 用的公网直链：视频段只认 URL（两个容器不共享路径）。"""
    if not OUT_TOKEN:
        raise SystemExit("缺 OUT_TOKEN 环境变量，视频没法发（.env 里有）")
    return f"https://zkysever.com/out/{video.name}?k={OUT_TOKEN}"


def enqueue(video: Path, text: str, url: str) -> str:
    payload = {
        "text": text,
        "video_url": url,
        "bvid": f"prep-{video.stem}",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-prep-{video.stem}.json"
    tmp = ROOT / "out" / name
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    r = run(["scp", "-q", "-P", "2222", "-i", SSH_KEY, tmp,
             f"root@118.178.109.54:{OUTBOX}/{name}"])
    tmp.unlink(missing_ok=True)
    if r.returncode != 0:
        raise SystemExit("投递单 scp 失败")
    return name


def sync_queue() -> None:
    """把 out/queue 的 JSON 同步到服务器——`/series` 系列页读的就是它。

    不同步的话服务器只知道「上一期之前」的排期；这一页是公开页、要发给别人看，
    数字和列表过期比空着更糟。
    """
    r = run(["scp", "-q", "-r", "-P", "2222", "-i", SSH_KEY,
             ROOT / "out" / "queue", "root@118.178.109.54:/opt/group-brain/out/"])
    if r.returncode != 0:
        log("  !! 排期没同步到服务器（/series 会少几期，不影响发片）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", default=None)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    ep = pick_episode(args.force)
    if not ep:
        log("今天没有该备的片（或者今天的已经发过了）")
        return 0
    log(f"今天备 {ep['id']}（{ep['date']}）")

    if args.dry:
        return 0

    video, cover, meta_path = build(ep)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    push_to_server([video, cover])
    sync_queue()
    url = public_url(video)
    log(f"已推到服务器：{url}")

    text = (
        f"今天这期备好了：{meta['title']}\n\n"
        f"{(meta.get('summary') or '').strip()}\n\n"
        f"在线看：{url}\n"
        f"封面也配好了。\n\n"
        f"回我一句「可以」我就去发布；要改哪里直接说。"
    )
    name = enqueue(video, text, url)
    log(f"已写投递单 {name}，QQ 插件 60 秒内把视频发给你")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
