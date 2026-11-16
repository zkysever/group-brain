"""B 站选片：先看简介和评论（免费），值得才「看视频」（贵），最后推到 QQ。

为什么分成两段
------------
用户的原话是「先看评论和简介，如果符合再观看视频……主要是有点贵」。B 站视频本身
没法真的看（没有画面理解能力），能看的是**页面上的文字**：标题、简介、简介里的
链接、评论区。所以这里把「看视频」定义成「用真浏览器把页面全量读一遍」，那一步
要经过主人电脑上的 Chromium、慢且贵，因此每天限 3 次。

便宜的信号（热门列表 + 评论接口）都是公开 JSON，不需要登录、不需要浏览器：
    https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1
    https://api.bilibili.com/x/v2/reply?type=1&oid=<aid>&sort=2&ps=10

三段漏斗
--------
1. 候选：热门列表拿 50 条，去掉已经处理过的；
2. 粗筛：按互动率（评论/播放、弹幕/播放）排序，取前 N 条拉评论；
3. 判断：把标题 + 简介 + 热评交给 DeepSeek 打分（1~5），4 分以上才推。

推送为什么写文件
----------------
group-brain 跑在 systemd 里，QQ 在 Docker 里的 AstrBot 里，两边没有共享的 HTTP
口子。但 `/opt/qqbot/astrbot-data` 同时是宿主目录和容器的 `/AstrBot/data`，
于是「写一个 json 到 outbox」就是最省事、最不会漏的投递方式；AstrBot 那边的
插件轮询它、发完移到 sent/。

限额（用户明确要求的）
----------------------
- 每小时最多推 1 条；
- 每天最多「看视频」（深读）3 次；
- 只在 08:00–23:00 之间推。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config as cfg
from . import db

POPULAR = "https://api.bilibili.com/x/web-interface/popular"
REPLY = "https://api.bilibili.com/x/v2/reply"
VIEW = "https://api.bilibili.com/x/web-interface/view"
VIDEO_URL = "https://www.bilibili.com/video/{bvid}"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CANDIDATES = int(os.environ.get("CURATOR_CANDIDATES", "50"))
SHORTLIST = int(os.environ.get("CURATOR_SHORTLIST", "8"))
COMMENTS = int(os.environ.get("CURATOR_COMMENTS", "8"))
DEEP_PER_DAY = getattr(cfg, "CURATOR_DEEP_PER_DAY", 3)
PUSH_PER_HOUR = getattr(cfg, "CURATOR_PUSH_PER_HOUR", 1)
WINDOW = getattr(cfg, "CURATOR_WINDOW", (8, 23))
SCORE_PUSH = getattr(cfg, "CURATOR_SCORE_PUSH", 4)
SCORE_DEEP = int(os.environ.get("CURATOR_SCORE_DEEP", "4"))
PAGE_CHARS = int(os.environ.get("CURATOR_PAGE_CHARS", "3000"))

OUTBOX = getattr(cfg, "CURATOR_OUTBOX", "/opt/qqbot/astrbot-data/curator/outbox")


# ---------------------------------------------------------------- 数据库

SCHEMA = """
CREATE TABLE IF NOT EXISTS curator_items (
    bvid      TEXT PRIMARY KEY,
    aid       INTEGER,
    title     TEXT NOT NULL,
    url       TEXT NOT NULL,
    tname     TEXT,
    owner     TEXT,
    descr     TEXT,
    view      INTEGER NOT NULL DEFAULT 0,
    reply     INTEGER NOT NULL DEFAULT 0,
    danmaku   INTEGER NOT NULL DEFAULT 0,
    like_cnt  INTEGER NOT NULL DEFAULT 0,
    duration  INTEGER NOT NULL DEFAULT 0,
    comments  TEXT,
    score     INTEGER,
    reason    TEXT,
    deep_at   TEXT,
    pushed_at TEXT,
    seen_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS curator_state (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_curator_pushed ON curator_items(pushed_at);
CREATE INDEX IF NOT EXISTS idx_curator_deep ON curator_items(deep_at);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- 取数

_COOKIE: str | None = None


def _uicookie() -> str:
    """B 站接口对「没有任何 cookie 的请求」会直接 412，随便编一个 buvid 就够。

    cookie 只要格式像，不会校验签名；同一个进程里复用，别每次换。
    """
    global _COOKIE
    if _COOKIE is None:
        import random

        hexs = lambda n: "".join(random.choice("0123456789ABCDEF") for _ in range(n))  # noqa: E731
        buvid = f"{hexs(8)}-{hexs(4)}-{hexs(4)}-{hexs(4)}-{hexs(12)}infoc"
        _COOKIE = f"buvid3={buvid}; buvid4={buvid}"
    return _COOKIE


def _get_json(url: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Cookie": _uicookie(),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def view(bvid: str) -> dict[str, Any]:
    """单个视频的元信息（标题、简介、时长、分区、UP、统计）。失败返回 {}。"""
    if not bvid:
        return {}
    try:
        data = _get_json(f"{VIEW}?bvid={bvid}")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 视频详情失败 {bvid}: {type(exc).__name__}: {exc}", flush=True)
        return {}
    if int((data or {}).get("code") or 0) != 0:
        print(f"[warn] 视频详情返回 code={(data or {}).get('code')} {bvid}", flush=True)
        return {}
    return (data or {}).get("data") or {}


def popular(limit: int = CANDIDATES) -> list[dict[str, Any]]:
    """热门列表。失败返回空列表，让上层当成「这小时没货」。"""
    out: list[dict[str, Any]] = []
    page = 1
    while len(out) < limit and page <= 3:
        try:
            data = _get_json(f"{POPULAR}?ps=50&pn={page}")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 热门列表第 {page} 页失败: {exc}", flush=True)
            break
        items = ((data or {}).get("data") or {}).get("list") or []
        if not items:
            break
        for it in items:
            st = it.get("stat") or {}
            out.append({
                "bvid": it.get("bvid") or "",
                "aid": it.get("aid") or 0,
                "title": (it.get("title") or "").strip(),
                "tname": (it.get("tname") or "").strip(),
                "owner": ((it.get("owner") or {}).get("name") or "").strip(),
                "descr": (it.get("desc") or "").strip(),
                "view": int(st.get("view") or 0),
                "reply": int(st.get("reply") or 0),
                "danmaku": int(st.get("danmaku") or 0),
                "like": int(st.get("like") or 0),
                "duration": int(it.get("duration") or 0),
            })
        page += 1
    return [x for x in out if x["bvid"]][:limit]


def comments(aid: int, limit: int = COMMENTS) -> list[str]:
    """热评（按点赞排序）。拿不到就算了——评论只是加分项，不该拖垮整轮。"""
    if not aid:
        return []
    try:
        data = _get_json(f"{REPLY}?type=1&oid={aid}&sort=2&ps={limit}&pn=1")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 评论接口失败 aid={aid}: {exc}", flush=True)
        return []
    reps = ((data or {}).get("data") or {}).get("replies") or []
    out = []
    for r in reps:
        msg = ((r.get("content") or {}).get("message") or "").strip()
        if not msg:
            continue
        msg = re.sub(r"\s+", " ", msg)[:120]
        out.append(f"[赞{r.get('like') or 0}] {msg}")
    return out


# ---------------------------------------------------------------- 判断

_SYSTEM = (
    "你在替一个 18 岁的学生挑 B 站视频。他喜欢：真实的技术/工程过程、有信息量的讲解、"
    "有意思的脑洞与纪录片、游戏解说里真有内容的那种。他不喜欢：纯标题党、纯剪辑卡点、"
    "重复的热搜凑数、搬运与合集拼盘。只输出严格 JSON，不要解释。"
)

_PROMPT = """下面是 B 站热门里的一条视频，请判断「这个人关掉视频之后，会不会觉得这 8 分钟没白花」。

标题：{title}
分区：{tname}　UP：{owner}　时长：{dur}　播放：{view}　评论：{reply}　弹幕：{danmaku}
简介：{descr}
热评（按点赞）：
{comments}

返回 JSON：
{{"score": 1-5 的整数, "reason": "不超过 40 字，说清好在哪或差在哪", "hook": "不超过 30 字，如果推给朋友你会怎么说"}}

评分标准：
5 = 有真东西，值得专门找时间看
4 = 有意思，随手推给他不亏
3 = 一般，可看可不看
2 = 水，标题大于内容
1 = 别浪费时间
注意：播放量高不等于好（热门里有大量凑数内容），评论区如果真的在讨论技术细节/剧情/事实，加分。"""


def _fmt(n: int) -> str:
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def _mmss(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def _llm(system: str, user: str, temperature: float = 0.3, timeout: float = 60) -> str:
    """调一次 DeepSeek，返回文本。

    不复用 `agent._chat`：那个函数每次都带 tools 和 tool_choice=auto，打分场景既不需要
    也多花 token；这里直接要 JSON 输出（response_format=json_object），省一次解析失败。
    """
    import requests

    from . import config as _cfg

    headers = {"Authorization": f"Bearer {_cfg.DEEPSEEK_KEY}"}
    payload = {
        "model": _cfg.DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(_cfg.DEEPSEEK_URL, json=payload, headers=headers, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"上游 {r.status_code}: {r.text[:120]}")
            r.raise_for_status()
            body = r.json()
            return ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            last = exc
            if attempt == 2:
                break
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def judge(item: dict[str, Any], cmts: list[str]) -> dict[str, Any]:
    """给一条候选打分。模型挂了就返回 0 分（宁可不推，也不要推一条没判断过的）。"""
    prompt = _PROMPT.format(
        title=item["title"], tname=item["tname"], owner=item["owner"],
        dur=_mmss(item["duration"]), view=_fmt(item["view"]),
        reply=_fmt(item["reply"]), danmaku=_fmt(item["danmaku"]),
        descr=(item["descr"] or "（无）")[:500],
        comments="\n".join(cmts) if cmts else "（没拿到评论）",
    )
    try:
        raw = (_llm(_SYSTEM, prompt) or "").strip()
        if not raw:
            raise ValueError("模型返回空")
        m = re.search(r"\{.*\}", raw, re.S)
        obj = json.loads(m.group(0)) if m else json.loads(raw)
        score = int(obj.get("score") or 0)
        return {
            "score": max(0, min(5, score)),
            "reason": str(obj.get("reason") or "")[:120],
            "hook": str(obj.get("hook") or "")[:60],
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 打分失败 {item['bvid']}: {type(exc).__name__}: {exc}", flush=True)
        return {"score": 0, "reason": "", "hook": ""}


# ---------------------------------------------------------------- 深读

def deep_read(bvid: str) -> str:
    """深读一条视频：优先走 B 站自己的接口（不需要浏览器、不需要登录、不占他电脑）。

    顺序：官方 view 接口拿标题/简介/时长/分区/UP/统计 → 热评接口 → 拼成一段文本。
    接口全挂时才退到 websearch.fetch（那条路可能要借真浏览器）。
    """
    parts: list[str] = []
    d = view(bvid)
    if d:
        st = d.get("stat") or {}
        dur = int(d.get("duration") or 0)
        parts.append(f"标题：{d.get('title') or ''}")
        parts.append(
            f"UP：{(d.get('owner') or {}).get('name') or ''}｜分区：{d.get('tname') or ''}｜"
            f"时长：{dur // 60}:{dur % 60:02d}"
        )
        parts.append(
            f"播放：{_fmt(int(st.get('view') or 0))}｜点赞：{_fmt(int(st.get('like') or 0))}｜"
            f"投币：{_fmt(int(st.get('coin') or 0))}｜收藏：{_fmt(int(st.get('favorite') or 0))}｜"
            f"弹幕：{_fmt(int(st.get('danmaku') or 0))}｜评论：{_fmt(int(st.get('reply') or 0))}"
        )
        descr = re.sub(r"\s+", " ", (d.get("desc") or "")).strip()
        if descr:
            parts.append(f"简介：{descr[:600]}")
        cmts = comments(int(d.get("aid") or 0), limit=COMMENTS)
        if cmts:
            parts.append("热评：" + " / ".join(c.replace("\n", " ") for c in cmts))

    text = "\n".join(parts)
    if text:
        return text[:PAGE_CHARS]

    # 兜底：接口一条都拿不到时才去抓页面
    print(f"[warn] 接口没拿到 {bvid}，退到抓页面", flush=True)
    from . import websearch

    try:
        page = websearch.fetch(VIDEO_URL.format(bvid=bvid), max_chars=PAGE_CHARS, timeout=25.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 深读失败 {bvid}: {type(exc).__name__}: {exc}", flush=True)
        return ""
    got = (page or {}).get("text") or ""
    return re.sub(r"[ \t]+", " ", got)[:PAGE_CHARS]


# ---------------------------------------------------------------- 限额

def in_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return WINDOW[0] <= now.hour < WINDOW[1]


def _last_pushed(conn) -> str:
    row = conn.execute("SELECT MAX(pushed_at) p FROM curator_items").fetchone()
    return (row["p"] or "") if row else ""


def pushed_within_hour(conn, now: datetime | None = None) -> bool:
    last = _last_pushed(conn)
    if not last:
        return False
    try:
        t = datetime.fromisoformat(last)
    except ValueError:
        return False
    now = now or datetime.now()
    return (now - t).total_seconds() < 3600


def deep_used_today(conn, now: datetime | None = None) -> int:
    now = now or datetime.now()
    day = now.strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) c FROM curator_items WHERE deep_at IS NOT NULL AND substr(deep_at,1,10)=?",
        (day,),
    ).fetchone()
    return int(row["c"] or 0) if row else 0


# ---------------------------------------------------------------- 投递

def queue_push(conn, item: dict[str, Any], deep_text: str = "") -> str:
    """把一条推送写成 json 文件，等 AstrBot 插件来取。返回文件路径。"""
    Path(OUTBOX).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "bvid": item["bvid"],
        "title": item["title"],
        "url": VIDEO_URL.format(bvid=item["bvid"]),
        "up": item.get("owner") or "",
        "tname": item.get("tname") or "",
        "score": item.get("score"),
        "reason": item.get("reason") or "",
        "hook": item.get("hook") or "",
        "comments": item.get("comments") or [],
        "deep": bool(deep_text),
    }
    text_lines = [f"🎬 {item['title']}"]
    if item.get("owner"):
        text_lines.append(f"UP：{item['owner']}　分区：{item.get('tname') or '-'}　{_mmss(item.get('duration') or 0)}")
    if item.get("hook"):
        text_lines.append(f"一句话：{item['hook']}")
    if item.get("reason"):
        text_lines.append(f"我的判断：{item['reason']}（{item.get('score')}/5）")
    cmts = item.get("comments") or []
    if cmts:
        text_lines.append("评论区：" + cmts[0][:80])
    text_lines.append(VIDEO_URL.format(bvid=item["bvid"]))
    payload["text"] = "\n".join(text_lines)

    path = Path(OUTBOX) / f"{stamp}-{item['bvid']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.execute(
        "UPDATE curator_items SET pushed_at=?, score=?, reason=? WHERE bvid=?",
        (payload["ts"], item.get("score"), item.get("reason") or "", item["bvid"]),
    )
    conn.commit()
    return str(path)


# ---------------------------------------------------------------- 主流程

def run(conn=None, force: bool = False, dry: bool = False) -> dict[str, Any]:
    """跑一轮选片。force 只跳过「窗口」与「每小时一条」，不跳过每天深读限额。"""
    own = conn is None
    conn = conn or db.connect()
    try:
        ensure_schema(conn)
        now = datetime.now()
        if not force and not in_window(now):
            return {"ok": True, "skipped": "不在推送窗口", "window": WINDOW}
        if not force and pushed_within_hour(conn, now):
            return {"ok": True, "skipped": "这一小时已经推过一条"}

        items = popular()
        known = {r["bvid"] for r in conn.execute("SELECT bvid FROM curator_items").fetchall()}
        fresh = [x for x in items if x["bvid"] not in known]
        if not fresh:
            return {"ok": True, "skipped": "没有新候选", "popular": len(items)}

        # 粗筛：互动率高的先看（评论/播放、弹幕/播放的比值，加一点绝对量）
        def heat(x: dict[str, Any]) -> float:
            v = max(x["view"], 1)
            return (x["reply"] / v) * 300 + (x["danmaku"] / v) * 200 + min(x["view"] / 1e6, 3.0)

        shortlist = sorted(fresh, key=heat, reverse=True)[:SHORTLIST]

        best: dict[str, Any] | None = None
        for it in shortlist:
            cmts = comments(it["aid"])
            verdict = judge(it, cmts)
            row = (it["bvid"], it["aid"], it["title"], VIDEO_URL.format(bvid=it["bvid"]),
                   it["tname"], it["owner"], it["descr"], it["view"], it["reply"],
                   it["danmaku"], it["like"], it["duration"], json.dumps(cmts, ensure_ascii=False),
                   verdict["score"], verdict["reason"], now.isoformat(timespec="seconds"))
            conn.execute(
                "INSERT INTO curator_items (bvid,aid,title,url,tname,owner,descr,view,reply,danmaku,"
                "like_cnt,duration,comments,score,reason,seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bvid) DO UPDATE SET score=excluded.score, reason=excluded.reason, seen_at=excluded.seen_at",
                row,
            )
            conn.commit()
            cand = dict(it, score=verdict["score"], reason=verdict["reason"],
                        hook=verdict["hook"], comments=cmts)
            if verdict["score"] >= SCORE_PUSH and (best is None or cand["score"] > best["score"]):
                best = cand
            if verdict["score"] >= 5:
                break  # 不用再看了，5 分就是天花板

        if best is None:
            return {"ok": True, "skipped": f"这轮没有到 {SCORE_PUSH} 分的", "checked": len(shortlist)}

        deep_text = ""
        if best["score"] >= SCORE_DEEP and deep_used_today(conn) < DEEP_PER_DAY:
            deep_text = deep_read(best["bvid"])
            if deep_text:
                conn.execute(
                    "UPDATE curator_items SET deep_at=? WHERE bvid=?",
                    (datetime.now().isoformat(timespec="seconds"), best["bvid"]),
                )
                conn.commit()

        if dry:
            return {"ok": True, "dry": True, "picked": best["bvid"], "score": best["score"],
                    "reason": best["reason"], "deep": bool(deep_text)}

        path = queue_push(conn, best, deep_text)
        return {"ok": True, "picked": best["bvid"], "score": best["score"],
                "reason": best["reason"], "deep": bool(deep_text), "outbox": path,
                "deep_left": max(0, DEEP_PER_DAY - deep_used_today(conn))}
    finally:
        if own:
            conn.close()


def pending(limit: int = 10) -> list[dict[str, Any]]:
    """还没被取走的推送文件（插件侧也可以用目录，这个接口给人看）。"""
    out: list[dict[str, Any]] = []
    d = Path(OUTBOX)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json"))[:limit]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return out


def history(conn, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT bvid,title,score,reason,deep_at,pushed_at,view,reply,seen_at "
        "FROM curator_items ORDER BY seen_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    force = "--force" in sys.argv
    res = run(force=force, dry=dry)
    print(json.dumps(res, ensure_ascii=False))
