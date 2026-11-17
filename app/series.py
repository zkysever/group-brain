"""系列页的数据源：把 out/queue 里「已经排期 / 已经发布」的每一期拼成一个列表。

为什么不靠 B 站合集：合集要电磁力 Lv2（粉丝 ≥100），现在 4 粉开不了。所以先自己做一页，
每期视频的简介都链过来——观众点一次就能顺着一集集看下去。
等合集能开了，这一页仍然有用（站外的目录）。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "out" / "queue"
BVID = re.compile(r"BV[0-9A-Za-z]{10}")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _published() -> dict[str, str]:
    st = _read_json(QUEUE / "state.json")
    return {str(k): str(v) for k, v in (st.get("published") or {}).items() if v}


def _num(ep: str) -> int:
    m = re.search(r"(\d+)$", ep)
    return int(m.group(1)) if m else 999


def episodes() -> dict:
    """返回 {"items": [...], "published": n, "total": n}。

    items 按集数排；只列「有 meta.json 的」，也就是真排期过的，不把占位符塞进去。
    """
    pub = _published()
    today = date.today().isoformat()
    items = []
    for d in sorted(QUEUE.glob("ep*"), key=lambda p: _num(p.name)):
        if not d.is_dir() or not (d / "meta.json").exists():
            continue
        meta = _read_json(d / "meta.json")
        ep = d.name
        # 服务器上的 state.json 可能还没来得及同步，所以 meta.json 里写过的 bvid 也算数
        bvid = pub.get(ep, "")
        if not bvid:
            m = BVID.search(str(meta.get("bvid") or ""))
            bvid = m.group(0) if m else ""
        when = str(meta.get("date") or "")
        title = str(meta.get("title") or "").split("｜")[0].strip() or ep.upper()
        items.append(
            {
                "id": ep,
                "no": _num(ep),
                "title": title,
                "date": when,
                "bvid": bvid,
                "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                "status": "published" if bvid else ("due" if when and when <= today else "upcoming"),
            }
        )
    return {
        "items": items,
        "published": sum(1 for i in items if i["bvid"]),
        "total": len(items),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
