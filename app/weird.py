"""抽象猎奇视频日报（A5）：每天 12:00 给用户推 10–20 条「让人怀疑人生」的 B 站链接。

和 `curator.py`（A1 的选片）是两条独立线：
- curator：从热门里挑「关掉不觉得白花时间」的有信息量的片，每条单独深读、发标题+理由；
- weird：主动去搜「抽象/整活/魔性」那一类短到离谱的片子，一次攒一批**只发链接**。

风格锚点（用户 2026-11-18 给的三条例子，已解析成 BV）：
    BV1YZxF69Ep3「真是一场酣畅淋漓的司凤啊」66 秒 / 242 万播放 / 鬼畜整活
    BV1Trs363EN2「《 光 速 入 土 》」17 秒 / 197 万播放 / 极短混剪
    BV1bbWd6PE2f「神之八秒（直视我！）」16 秒 / 5 万播放 / 莫名奇妙
共同点：**十几秒到一分钟、标题本身就是怪话、看完让人「啊？这是什么」**。

设计取舍：
- 候选池靠关键词搜（search/type 接口，order=click/dm，duration=1 即 10 分钟以内），
  不做热门遍历——热门里抽象片比例太低。
- 一次模型调用把 60–80 条标题排序，而不是逐条打分：20 条 × 一次调用太贵也太慢。
- 见过的 bvid 全部记进 `weird_items`，不重复推；模型挂了用启发式兜底，宁可少发也不空手。
- 只发链接（用户明确要求），所以 payload 的 text 就是若干行 URL，不带别的话。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from . import config as cfg
from .curator import _llm

SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
VIDEO_URL = "https://www.bilibili.com/video/{bvid}"
OUTBOX = getattr(cfg, "CURATOR_OUTBOX", "/opt/qqbot/astrbot-data/curator/outbox")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 搜索词：抽象/整活/魔性/离谱 四个方向各来几个，避免词面太像导致结果重复
KEYWORDS = [
    "抽象", "整活", "离谱", "猎奇", "魔性", "鬼畜",
    "沙雕", "逆天", "怀疑人生", "精神污染", "音MAD", "人类迷惑行为",
]
ORDERS = ("click", "dm")     # 按播放 / 按弹幕，两种口味轮着搜

MIN_SEC = 8                  # 低于 8 秒基本抓不到什么
MAX_SEC = 300                # 超过 5 分钟就不「短」了，也筛掉长视频
MIN_VIEW = 20_000            # 太冷门的没意思
MAX_VIEW = 10_000_000        # 太爆的往往是热搜搬运
POOL_CAP = 80                # 送进模型排序的候选上限
PICK_MIN, PICK_MAX = 10, 20

# 标题里出现这些词基本不是「抽象」，直接丢（省钱省时间）
BAD_WORDS = ("合集", "搬运", "教程", "攻略", "解说", "预告", "花絮", "发布会",
             "直播回放", "课程", "招聘", "广告", "带货", "测评")

# 内容红线：涉政、擦边、血腥自伤一律不进池子。
# 2026-11-18 实测：模型会挑「蒋介石童养媳」这种伪史整活，虽然技术上很「抽象」，
# 但这号的定位是学生作品，不能往上贴这类东西——所以放在**送模型之前**过滤。
BLOCK_TITLE = re.compile(
    "|".join((
        r"习近平|习大大|毛泽东|毛主席|邓小平|江泽民|胡锦涛|蒋介石|周恩来|彭丽媛",
        r"六四|天安门事件|文革|反右|法轮|新疆|西藏独立|港独|台独|参选|选举|公示",
        r"萝莉|幼女|色情|裸|脱衣|援交|约炮|黄片|av女",
        r"自杀|自残|自伤|割腕|跳楼|上吊|血腥|肢解|尸体|谋杀现场",
        r"纳粹|希特勒|屠杀|种族灭绝",
    ))
)

_SYSTEM = (
    "你在替一个 18 岁的学生挑 B 站「抽象 / 猎奇 / 离谱 / 让人怀疑人生」的短视频。"
    "他给的三个样本：\n"
    "  1)《真是一场酣畅淋漓的司凤啊》66 秒，鬼畜整活，242 万播放\n"
    "  2)《 光 速 入 土 》17 秒，极短混剪+谐音梗，197 万播放\n"
    "  3)《神之八秒（直视我！）》16 秒，莫名其妙的一张脸，5 万播放\n"
    "共同点：十几秒到一分钟、标题本身就是怪话、看完的反应是「啊？这是什么」。\n"
    "要的：整活、鬼畜、魔性混剪、离谱实证、抽象表演、标题就很怪的短片。\n"
    "不要的：影视解说、游戏实况、教程科普、广告带货、纯卡点剪辑、搬运长片；\n"
    "也不要：血腥、恶心、软色情、政治敏感、自伤内容——再抽象也不要。\n"
    "只输出严格 JSON，不要解释。"
)

_PROMPT = """下面是 {n} 条候选（编号 / 时长 / 播放 / 分区 / 标题）。请挑出 {lo}–{hi} 条最符合上述「抽象」口味的，
按「越怪越靠前」排序。宁可选没人看但真怪的，也不要热门但正常的。

{lines}

输出格式：{{"picks": [{{"i": 编号, "why": "不超过 20 字，说明哪里怪"}}]}}"""


# ---------------------------------------------------------------- 基础

def _secs(dur: Any) -> int:
    """把 search 接口的 '01:06' / '1:02:03' 换成秒。"""
    s = str(dur or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    parts = [p for p in s.split(":") if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    total = 0
    for n in nums:
        total = total * 60 + n
    return total


def _clean(title: str) -> str:
    return re.sub(r"<[^>]+>", "", title or "").strip()


def _get(params: dict) -> dict:
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    for attempt in range(3):
        try:
            r = requests.get(SEARCH, params=params, headers=headers, timeout=20)
            if r.status_code in (412, 429, 500, 502, 503):
                time.sleep(2 + attempt * 2)
                continue
            return r.json()
        except Exception:  # noqa: BLE001
            time.sleep(1 + attempt)
    return {}


# ---------------------------------------------------------------- 候选池

def pool() -> list[dict[str, Any]]:
    """按关键词各搜一遍，返回过滤后的候选（未去重排序）。"""
    out: dict[str, dict[str, Any]] = {}
    for kw in KEYWORDS:
        for order in ORDERS:
            for page in (1, 2):
                d = _get({"search_type": "video", "keyword": kw, "page": page,
                          "order": order, "duration": 1})
                for v in ((d.get("data") or {}).get("result") or []):
                    bv = v.get("bvid")
                    if not bv or bv in out:
                        continue
                    title = _clean(v.get("title"))
                    if not title or any(w in title for w in BAD_WORDS):
                        continue
                    if BLOCK_TITLE.search(title):
                        continue
                    sec = _secs(v.get("duration"))
                    if not (MIN_SEC <= sec <= MAX_SEC):
                        continue
                    view = int(v.get("play") or 0)
                    if not (MIN_VIEW <= view <= MAX_VIEW):
                        continue
                    out[bv] = {
                        "bvid": bv,
                        "title": title,
                        "tname": v.get("typename") or "",
                        "owner": v.get("author") or "",
                        "duration": sec,
                        "view": view,
                        "reply": int(v.get("video_review") or 0),
                        "danmaku": int(v.get("danmaku") or 0),
                        "tag": (v.get("tag") or "")[:80],
                        "descr": _clean(v.get("description") or "")[:160],
                        "pubdate": int(v.get("pubdate") or 0),
                    }
                time.sleep(0.4)
    return list(out.values())


def _heat(x: dict[str, Any]) -> float:
    """排序用的小分数：弹幕率 + 评论率，再给一点点绝对量。"""
    v = max(int(x["view"]), 1)
    return (x["danmaku"] / v) * 400 + (x["reply"] / v) * 300 + min(v / 1e6, 2.0)


def shortlist(items: list[dict[str, Any]], cap: int = POOL_CAP) -> list[dict[str, Any]]:
    return sorted(items, key=_heat, reverse=True)[:cap]


# ---------------------------------------------------------------- 排序

def pick(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """让模型从候选里挑一批。返回 (选中的条目, 'llm' | 'fallback')。"""
    if not items:
        return [], "empty"
    lines = "\n".join(
        f"{i}. {x['duration']}s / {x['view'] // 10000}万 / {x['tname'] or '-'} / {x['title'][:48]}"
        for i, x in enumerate(items)
    )
    prompt = _PROMPT.format(n=len(items), lo=PICK_MIN, hi=PICK_MAX, lines=lines)
    try:
        raw = (_llm(_SYSTEM, prompt, temperature=0.5, timeout=90) or "").strip()
        m = re.search(r"\{.*\}", raw, re.S)
        obj = json.loads(m.group(0) if m else raw)
        picks = []
        for p in obj.get("picks") or []:
            try:
                i = int(p.get("i"))
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(items):
                it = dict(items[i], why=str(p.get("why") or "")[:40], score=5)
                picks.append(it)
        # 去重（模型偶尔同一个编号写两遍）
        seen: set[str] = set()
        uniq = []
        for it in picks:
            if it["bvid"] in seen:
                continue
            seen.add(it["bvid"])
            uniq.append(it)
        if uniq:
            return uniq[:PICK_MAX], "llm"
        print("[warn] 模型没给出可用编号，走兜底", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 排序失败 {type(exc).__name__}: {exc}", flush=True)

    # 兜底：按弹幕率取前若干条，至少别让当天断供
    fallback = [dict(x, why="兜底：弹幕率靠前", score=3) for x in items[:PICK_MAX]]
    return fallback, "fallback"


def _fill(picked: list[dict[str, Any]], cand: list[dict[str, Any]],
          lo: int = PICK_MIN) -> list[dict[str, Any]]:
    """不够 lo 条就从剩下的候选里按热度补足。

    为什么需要：模型偶尔只挑五六条（或者挑中的被红线拦掉），而这条线的承诺是
    「一次 10–20 条」。少发比多发更糟 —— 用户每天早上打开就是这个数量，少了他会以为坏了。
    """
    out = list(picked)
    have = {x["bvid"] for x in out}
    if len(out) >= lo:
        return out
    for x in cand:
        if len(out) >= lo:
            break
        if x["bvid"] in have or BLOCK_TITLE.search(x["title"]):
            continue
        have.add(x["bvid"])
        out.append(dict(x, why="补足数量（按弹幕率）", score=3))
    return out


# ---------------------------------------------------------------- 存储

SCHEMA = """
CREATE TABLE IF NOT EXISTS weird_items (
    bvid      TEXT PRIMARY KEY,
    title     TEXT,
    url       TEXT,
    tname     TEXT,
    owner     TEXT,
    duration  INTEGER,
    view      INTEGER,
    reply     INTEGER,
    danmaku   INTEGER,
    score     INTEGER,
    reason    TEXT,
    batch     TEXT,
    seen_at   TEXT,
    pushed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_weird_pushed ON weird_items(pushed_at);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def seen(conn: sqlite3.Connection) -> set[str]:
    return {r["bvid"] for r in conn.execute("SELECT bvid FROM weird_items").fetchall()}


def pushed_today(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) c FROM weird_items WHERE pushed_at IS NOT NULL AND substr(pushed_at,1,10)=?",
        (day,),
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def push(conn: sqlite3.Connection, items: list[dict[str, Any]], dry: bool = False) -> str:
    """写投递单（只发链接，一行一条）。返回文件路径（dry 时为空串）。"""
    batch = "weird-" + datetime.now().strftime("%Y%m%d-%H%M")
    text = "\n".join(VIDEO_URL.format(bvid=x["bvid"]) for x in items)
    if dry:
        return ""
    Path(OUTBOX).mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "bvid": batch,
        "text": text,
        "count": len(items),
        "items": [{"bvid": x["bvid"], "title": x["title"], "why": x.get("why") or ""} for x in items],
    }
    path = Path(OUTBOX) / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{batch}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    now = datetime.now().isoformat(timespec="seconds")
    for x in items:
        conn.execute(
            "INSERT INTO weird_items (bvid,title,url,tname,owner,duration,view,reply,danmaku,"
            "score,reason,batch,seen_at,pushed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(bvid) DO UPDATE SET score=excluded.score, reason=excluded.reason, "
            "batch=excluded.batch, pushed_at=excluded.pushed_at",
            (x["bvid"], x["title"], VIDEO_URL.format(bvid=x["bvid"]), x.get("tname") or "",
             x.get("owner") or "", x["duration"], x["view"], x["reply"], x["danmaku"],
             x.get("score") or 0, x.get("why") or "", batch, now, now),
        )
    conn.commit()
    return str(path)


def history(conn: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT bvid,title,url,duration,view,reason,batch,pushed_at FROM weird_items "
        "WHERE pushed_at IS NOT NULL ORDER BY pushed_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 主流程

def run(conn: sqlite3.Connection | None = None, force: bool = False,
        dry: bool = False) -> dict[str, Any]:
    from . import db

    own = conn is None
    conn = conn or db.connect()
    try:
        ensure_schema(conn)
        if not force and pushed_today(conn) > 0:
            return {"ok": True, "skipped": "今天已经推过一批", "pushed_today": pushed_today(conn)}

        raw = pool()
        known = seen(conn)
        fresh = [x for x in raw if x["bvid"] not in known]
        if not fresh:
            return {"ok": True, "skipped": "没有新候选", "pool": len(raw)}

        cand = shortlist(fresh)
        picked, how = pick(cand)
        # 模型偶尔还是会挑到红线内容（prompt 说不听），发之前再拦一道
        picked = [x for x in picked if not BLOCK_TITLE.search(x["title"])]
        picked = _fill(picked, cand)
        if not picked:
            return {"ok": True, "skipped": "挑不出东西", "pool": len(raw), "cand": len(cand)}
        if dry:
            return {"ok": True, "dry": True, "how": how, "pool": len(raw), "cand": len(cand),
                    "picked": [{"bvid": x["bvid"], "title": x["title"], "why": x.get("why")}
                               for x in picked]}
        path = push(conn, picked, dry=False)
        return {"ok": True, "how": how, "pool": len(raw), "cand": len(cand),
                "count": len(picked), "outbox": path,
                "titles": [x["title"][:30] for x in picked[:5]]}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    print(json.dumps(run(force="--force" in os.sys.argv, dry="--dry" in os.sys.argv),
                     ensure_ascii=False, indent=2))
