"""群里听不懂的词：先上网查，再记住（A6，2026-11-19）

用户的要求（2026-11-19 原话要点）
--------------------------------
「写一个优化插件，能够让你自群里的它自主学习，在碰到听不懂的话时可以让你上网查找，
  但我要求是做在云端，也就是不要被我的电脑是否开机限制。」

所以这套东西全部跑在服务器上：AstrBot（收消息、回复）和 group-brain（判词、查网、存词）
都在这台机器里，他电脑关着也照跑；需要他电脑的只有投稿那套 bsk（跟这里无关）。

为什么不用「词典里没有就算新词」
------------------------------
中文没有一份能用的新词表，硬套只会把「这个」「真的」「行吧」全标成新词。真正可靠的
信号只有一个：**模型自己说它不认识**。所以流程是反的 —— 先让它读完这一阵群聊，
问它「这里面哪几个词你不确定」，它挑出来的才值得花一次搜索。

四段流水线
----------
1. 收集：AstrBot 插件把群消息 POST /api/lexicon/observe（后台线程，不挡回复）
2. 判词：模型从这一段里挑 ≤3 个「我不确定」的词，并要求给出它出现的那句话
3. 查网：`websearch.search("{词} 什么意思")`，再用模型把摘要压成 30 字以内的解释；
         查不到、或模型自己说不知道 → **不写库**（宁缺勿滥），只记一条失败 job
4. 使用：`/api/lexicon/context` 把命中的解释给回复插件，拼进 prompt

为什么不直接把搜索结果塞给群友
------------------------------
搜索摘要里一半是广告和搬运站，人看了会烦。这里只用它来「学会」一个词，出口是一句话解释。

限额
----
一条群聊里可能一次冒出五六个新词，不设限一天就能把搜索额度烧光。所以：
每天最多记 `LEX_MAX_TERMS_DAY` 个新词、真正上网 `LEX_MAX_LOOKUPS_DAY` 次；
同一个词最多试 `MAX_ATTEMPTS` 次，学不会就别再浪费。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any

from . import config as cfg
from . import websearch
from .curator import _llm

# 一天最多记几个新词 / 最多真正上网查几次
MAX_TERMS_DAY = int(getattr(cfg, "LEX_MAX_TERMS_DAY", 40))
MAX_LOOKUPS_DAY = int(getattr(cfg, "LEX_MAX_LOOKUPS_DAY", 12))
MAX_ATTEMPTS = 2                 # 同一个词最多查几次，学不会就放弃
CONF_USE = 0.45                  # 低于这个置信度不拿去用（也不删，留着看）
CONF_STORE = 0.40                # 低于这个干脆不入库
CONF_GUESS = 0.35                # 只凭上下文猜出来的，置信度封顶在这里（永远低于 CONF_USE）
MAX_TERM_LEN = 12
MAX_GLOSS_LEN = 120
MAX_CONTEXT_LEN = 60

# 这些不是「听不懂的词」，是汉语的胶水。判词时模型偶尔会挑它们，挡一道。
STOP = {
    "这个", "那个", "什么", "怎么", "为什么", "就是", "然后", "可以", "不是", "没有",
    "我们", "你们", "他们", "自己", "时候", "现在", "刚才", "还是", "这样", "那样",
    "感觉", "觉得", "真的", "其实", "应该", "可能", "知道", "一个", "一下", "一会",
    "东西", "事情", "问题", "哈哈", "哈哈哈", "嗯嗯", "好的", "行吧", "确实",
}

_CLEAN = re.compile(r"[\s\"'“”‘’《》()（）\[\]【】,，.。:：;；!！?？~～…、/\\|]+")

_JUDGE_SYSTEM = """你是一个中文 QQ 群里的 AI 助手，正在复盘自己刚看到的一段群聊。

你的任务：找出这段聊天里**你确实不确定含义**的词（网络新词、梗、缩写、群内外号、
外来语、错别字梗都算）。要求：

1. 只列你真的不确定的，最多 3 个；一个都没有就返回空数组。
2. 不要列「这个」「什么」这类常用词，也不要列人名（除非是明显的谐音梗外号）。
3. 每个词给你是从哪句话里看到的（原话，≤30 字）。

只输出 JSON：{"terms":[{"term":"词","why":"为什么不确定","quote":"原话"}]}"""

_GLOSS_SYSTEM = """你正在给一个 QQ 群机器人维护一份「群内词库」。现在给你一个词和几条网页摘要，
请写一句解释，供机器人以后看懂这个词。

规则：
1. 只用材料里有的信息，不要编。材料里说不清就返回 confidence 0。
2. 解释要短：≤30 字，说清「这个词指什么」，必要时补一句来源（哪个圈子/哪个作品）。
3. 如果这是个骂人 / 涉政 / 软色情的词，也要如实说明它是什么，但不要复述内容。
4. confidence 是你对这条解释的把握，0~1。

只输出 JSON：{"gloss":"一句话解释","kind":"梗|缩写|外号|术语|其它","confidence":0.0}"""

_MINE_SYSTEM = """你在帮一个 QQ 群机器人整理「这个群自己才懂的说法」。

从下面这段群聊里挑出 1~8 个**圈外人可能看不懂**的词：群内外号、谐音梗、
他们自己造的说法、特定圈子的黑话、反复出现的缩写。要求：

1. 优先挑重复出现、或者明显是「这个群专属」的；
2. 不要挑常用词、也不要挑通用网络梗（那种上网一搜满屏都是）；
3. 给出每个词出现时的原话（≤30 字）。

只输出 JSON：{"terms":[{"term":"词","quote":"原话","why":"为什么觉得是群内说法"}]}"""

# 只凭上下文猜（外号、群内自造词搜网是搜不到的）。
# 这条路径产出**一律低置信度、默认不给模型用**，等人点头才算数 ——
# 宁可让词躺在待审里，也别让它在群聊里自信地用错一个外号。
_GUESS_SYSTEM = """一个 QQ 群里的 AI 助手碰到一个不懂的词，只看到它出现的那句话，没有别的材料。

请只根据这句话，推测这个词在**这个群里**大概指什么。规则：

1. 不许编细节：拿不准就写得更笼统（比如「群里对某位成员的称呼」）。
2. 解释 ≤25 字。
3. confidence 要诚实：只凭一句话猜，通常不会高。
4. 完全看不出任何线索时返回 gloss 空字符串。

只输出 JSON：{"gloss":"一句话猜测","kind":"外号|梗|缩写|术语|其它","confidence":0.0}"""


# --------------------------------------------------------------------- 基础

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def ensure(conn: sqlite3.Connection) -> None:
    """建表。放在这里而不是 db.SCHEMA：这个模块是后加的，老库不该因为升级脚本顺序而报错。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lexicon (
            term       TEXT PRIMARY KEY,
            gloss      TEXT NOT NULL,
            kind       TEXT NOT NULL DEFAULT '梗',
            source     TEXT NOT NULL DEFAULT 'web',
            urls       TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0.5,
            hits       INTEGER NOT NULL DEFAULT 0,
            asked      INTEGER NOT NULL DEFAULT 0,
            status     TEXT NOT NULL DEFAULT 'auto',
            first_seen TEXT NOT NULL,
            updated    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lexicon_jobs (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            ts   TEXT NOT NULL,
            term TEXT NOT NULL,
            ok   INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_lexicon_jobs_ts ON lexicon_jobs(ts);
        """
    )
    conn.commit()


def clean_term(term: str) -> str:
    """去掉标点、空白、引号。判词模型很爱把词连标点一起吐出来。"""
    return _CLEAN.sub("", (term or "").strip())[:MAX_TERM_LEN]


def _usable(term: str) -> bool:
    if not term or len(term) < 2 or len(term) > MAX_TERM_LEN:
        return False
    if term in STOP:
        return False
    # 纯数字也算词：这个群里的成员外号就有叫「147」的，一律当噪音会漏掉真词。
    # 但太长的数字串（日期、QQ 号）没意义。
    if term.isdigit() and len(term) > 6:
        return False
    return True


def _used_today(conn: sqlite3.Connection) -> int:
    """今天真正学会（上网成功）几个词 —— 这是花钱的那一项，必须单独计。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM lexicon_jobs WHERE ts >= ? AND ok = 1",
        (_today() + " 00:00:00",),
    ).fetchone()
    return int(row[0] or 0)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure(conn)
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(status='confirmed') c, SUM(status='rejected') r FROM lexicon"
    ).fetchone()
    today = conn.execute(
        "SELECT COUNT(*) FROM lexicon WHERE first_seen >= ?", (_today() + " 00:00:00",)
    ).fetchone()[0]
    jobs = conn.execute(
        "SELECT SUM(ok=1), COUNT(*) FROM lexicon_jobs WHERE ts >= ?", (_today() + " 00:00:00",)
    ).fetchone()
    return {
        "terms": int(row["n"] or 0),
        "confirmed": int(row["c"] or 0),
        "rejected": int(row["r"] or 0),
        "new_today": int(today or 0),
        "learned_today": int(jobs[0] or 0),
        "jobs_today": int(jobs[1] or 0),
    }


# --------------------------------------------------------------------- 查网

def _snippets(conn, term: str, quote: str = "") -> list[dict[str, str]]:
    """搜这个词。先按「什么意思」搜，没有就裸搜 —— 很多梗名裸搜才出结果。

    带上原句一起搜是必要的：光搜「香香软软的小猫娘」会命中一堆美妆页面，
    混进原句里的其他词（谁说的、在骂谁）才能把结果拉回群里的意思。
    """
    queries = [f"{term} 什么意思", f"{term} 是什么梗"]
    if quote and term in quote:
        tail = quote.split(term, 1)[1].strip(" ：:，,。.！!？?")[:18]
        if tail:
            queries.insert(0, f"{term} {tail}")
    queries.append(term)
    for q in queries:
        try:
            hits = websearch.search(conn, q, limit=4)
        except Exception:  # noqa: BLE001 —— 搜索挂了不该让整条流水线炸
            hits = []
        if hits:
            return hits
    return []


def lookup(conn: sqlite3.Connection, term: str, quote: str = "") -> dict[str, Any] | None:
    """真正学一个词：搜 → 让模型写一句解释 → 写库；搜不到就退化成「只凭上下文猜」。

    返回写进去的那一行；什么都学不出来返回 None（调用方据此记 job 失败）。
    """
    term = clean_term(term)
    if not _usable(term):
        return None
    ensure(conn)

    row = conn.execute("SELECT asked FROM lexicon WHERE term = ?", (term,)).fetchone()
    if row and int(row["asked"]) >= MAX_ATTEMPTS:
        return None

    hits = _snippets(conn, term, quote)
    if not hits:
        _job(conn, term, False, "搜不到")
        return _guess(conn, term, quote)

    material = "\n".join(
        f"- {h.get('title', '')[:60]}：{(h.get('snippet') or '')[:160]}" for h in hits[:4]
    )
    user = (
        f"词：{term}\n"
        + (f"它出现在这句话里：{quote[:MAX_CONTEXT_LEN]}\n" if quote else "")
        + f"\n网页摘要：\n{material}"
    )
    try:
        raw = _llm(_GLOSS_SYSTEM, user, temperature=0.1, timeout=45)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _job(conn, term, False, f"模型失败 {type(exc).__name__}")
        return _guess(conn, term, quote)

    gloss = (data.get("gloss") or "").strip()[:MAX_GLOSS_LEN]
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if not gloss or "不知道" in gloss[:6] or conf < CONF_STORE:
        _job(conn, term, False, "模型说不确定")
        # 网页讲不明白的词，多半是群内外号 —— 这条线还可以靠上下文猜一手
        return _guess(conn, term, quote)

    kind = (data.get("kind") or "梗").strip()[:8]
    urls = json.dumps([h.get("url", "") for h in hits[:3] if h.get("url")], ensure_ascii=False)
    now = _now()
    conn.execute(
        "INSERT INTO lexicon (term, gloss, kind, source, urls, confidence, hits, asked,"
        " status, first_seen, updated) VALUES (?, ?, ?, 'web', ?, ?, 0, 1, 'auto', ?, ?)"
        " ON CONFLICT(term) DO UPDATE SET gloss = excluded.gloss, kind = excluded.kind,"
        " urls = excluded.urls, confidence = excluded.confidence,"
        " asked = lexicon.asked + 1, updated = excluded.updated",
        (term, gloss, kind, urls, conf, now, now),
    )
    _job(conn, term, True, f"conf={conf:.2f}")
    conn.commit()
    return get(conn, term)


def _guess(conn: sqlite3.Connection, term: str, quote: str) -> dict[str, Any] | None:
    """搜不到材料时的兜底：只凭原句猜，写成 status='guess' 躺进待审。

    为什么要有这条：群里真正天天在用的词（外号、自造说法）在网上是没有词条的，
    只走「先搜后写」会永远学不到它们。但猜也不能当成事实 —— 所以：
    置信度封顶 0.35、status 记 guess、`glosses_for` 不用它，等人 confirm 之后才生效。
    """
    if not quote:
        return None
    try:
        raw = _llm(
            _GUESS_SYSTEM,
            f"词：{term}\n它出现在这句话里：{quote[:MAX_CONTEXT_LEN]}",
            temperature=0.2,
            timeout=30,
        )
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        _job(conn, term, False, "猜也没猜出来")
        return None
    gloss = (data.get("gloss") or "").strip()[:MAX_GLOSS_LEN]
    if not gloss:
        _job(conn, term, False, "猜不出（无上下文线索）")
        return None
    try:
        conf = min(float(data.get("confidence") or 0), CONF_GUESS)
    except (TypeError, ValueError):
        conf = 0.0
    if conf <= 0:
        _job(conn, term, False, "猜不出（置信为 0）")
        return None
    kind = (data.get("kind") or "外号").strip()[:8]
    now = _now()
    conn.execute(
        "INSERT INTO lexicon (term, gloss, kind, source, urls, confidence, hits, asked,"
        " status, first_seen, updated) VALUES (?, ?, ?, 'group', '[]', ?, 0, 1, 'guess', ?, ?)"
        " ON CONFLICT(term) DO UPDATE SET gloss = excluded.gloss, kind = excluded.kind,"
        " source = 'group', confidence = excluded.confidence,"
        " asked = lexicon.asked + 1, updated = excluded.updated",
        (term, gloss, kind, conf, now, now),
    )
    _job(conn, term, True, f"guess conf={conf:.2f}（待人点头）")
    conn.commit()
    return get(conn, term)


def _job(conn: sqlite3.Connection, term: str, ok: bool, note: str) -> None:
    conn.execute(
        "INSERT INTO lexicon_jobs (ts, term, ok, note) VALUES (?, ?, ?, ?)",
        (_now(), term, 1 if ok else 0, note[:120]),
    )
    # 只留最近 2000 条，判词日志别无限长
    conn.execute(
        "DELETE FROM lexicon_jobs WHERE id NOT IN (SELECT id FROM lexicon_jobs ORDER BY id DESC LIMIT 2000)"
    )
    conn.commit()


# --------------------------------------------------------------------- 读写

def get(conn: sqlite3.Connection, term: str) -> dict[str, Any] | None:
    ensure(conn)
    row = conn.execute("SELECT * FROM lexicon WHERE term = ?", (clean_term(term),)).fetchone()
    return dict(row) if row else None


def all_rows(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    ensure(conn)
    rows = conn.execute(
        "SELECT * FROM lexicon ORDER BY updated DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def glosses_for(conn: sqlite3.Connection, text: str, limit: int = 3) -> list[dict[str, Any]]:
    """这句话里出现了哪些已经学会的词。不做分词：词库只有几百条，逐条查子串最快。"""
    text = text or ""
    if not text:
        return []
    ensure(conn)
    rows = conn.execute(
        "SELECT * FROM lexicon WHERE confidence >= ? AND status IN ('auto', 'confirmed')"
        " ORDER BY confidence DESC, hits DESC LIMIT 400",
        (CONF_USE,),
    ).fetchall()
    out = []
    for r in rows:
        term = r["term"]
        if term and term in text:
            out.append(dict(r))
            if len(out) >= limit:
                break
    if out:
        conn.executemany(
            "UPDATE lexicon SET hits = hits + 1 WHERE term = ?",
            [(x["term"],) for x in out],
        )
        conn.commit()
    return out


def hint(conn: sqlite3.Connection, text: str, limit: int = 3) -> str:
    """给 prompt 用的一行提示；没有命中返回空串。"""
    hits = glosses_for(conn, text, limit=limit)
    if not hits:
        return ""
    parts = [f"{h['term']}={h['gloss']}" for h in hits]
    return "群内词库（群里自己积累的说法，回答时可以直接用）：" + "；".join(parts)


def correct(conn: sqlite3.Connection, term: str, gloss: str) -> int:
    """人工/群友纠正：把某个词的解释覆盖掉，置信度拉满。"""
    term = clean_term(term)
    gloss = (gloss or "").strip()[:MAX_GLOSS_LEN]
    if not _usable(term) or not gloss:
        raise ValueError("词或解释为空")
    ensure(conn)
    now = _now()
    conn.execute(
        "INSERT INTO lexicon (term, gloss, kind, source, confidence, status, first_seen, updated)"
        " VALUES (?, ?, '梗', 'human', 1.0, 'confirmed', ?, ?)"
        " ON CONFLICT(term) DO UPDATE SET gloss = excluded.gloss, confidence = 1.0,"
        " status = 'confirmed', source = 'human', updated = excluded.updated",
        (term, gloss, now, now),
    )
    conn.commit()
    return 1


def confirm(conn: sqlite3.Connection, term: str, gloss: str = "") -> int:
    """人点头：把「猜的」变成「能用的」。

    `_guess()` 产出的词置信度封顶 0.35，`glosses_for` 不会用它 —— 这是故意的：
    群里叫错一个外号，比不认识这个词尴尬得多。看到待审觉得对，就调这个接口放行；
    顺手把 gloss 传进来还能改一个字。
    """
    term = clean_term(term)
    if not _usable(term):
        raise ValueError("词为空")
    ensure(conn)
    row = get(conn, term)
    if not row:
        return 0
    new_gloss = (gloss or "").strip()[:MAX_GLOSS_LEN] or row["gloss"]
    cur = conn.execute(
        "UPDATE lexicon SET gloss = ?, status = 'confirmed', confidence = 0.9,"
        " source = 'human', updated = ? WHERE term = ?",
        (new_gloss, _now(), term),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def reject(conn: sqlite3.Connection, term: str) -> int:
    """学错了：标成 rejected，以后不再返回（也不删，方便回看）。"""
    ensure(conn)
    cur = conn.execute(
        "UPDATE lexicon SET status = 'rejected', updated = ? WHERE term = ?",
        (_now(), clean_term(term)),
    )
    conn.commit()
    return int(cur.rowcount or 0)


# --------------------------------------------------------------------- 判词

def judge(lines: list[dict[str, str]], reply: str = "") -> list[dict[str, str]]:
    """从一段群聊里挑出「我不确定」的词。没挑到返回空列表。"""
    text = "\n".join(f"{(x.get('who') or '某人')}：{(x.get('text') or '').strip()}" for x in lines)
    text = text.strip()
    if len(text) < 4:
        return []
    user = "群聊：\n" + text[:1500]
    if reply:
        user += f"\n\n（我当时回的是：{reply[:200]}）"
    try:
        raw = _llm(_JUDGE_SYSTEM, user, temperature=0.2, timeout=40)
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in (data.get("terms") or [])[:3]:
        term = clean_term(str(item.get("term") or ""))
        if _usable(term):
            out.append(
                {
                    "term": term,
                    "why": str(item.get("why") or "")[:MAX_CONTEXT_LEN],
                    "quote": str(item.get("quote") or "")[:MAX_CONTEXT_LEN],
                }
            )
    return out


def learn(conn: sqlite3.Connection, terms: list[dict[str, str]]) -> dict[str, Any]:
    """把判出来的词一个个查网学掉（受每日限额约束）。"""
    ensure(conn)
    got, skipped = [], []
    today_terms = conn.execute(
        "SELECT COUNT(*) FROM lexicon WHERE first_seen >= ?", (_today() + " 00:00:00",)
    ).fetchone()[0]
    today_lookups = _used_today(conn)
    for item in terms:
        term = clean_term(item.get("term", ""))
        if not _usable(term):
            continue
        if get(conn, term):
            skipped.append({"term": term, "why": "已经会了"})
            continue
        if int(today_terms) >= MAX_TERMS_DAY or today_lookups >= MAX_LOOKUPS_DAY:
            skipped.append({"term": term, "why": "今日额度用完"})
            continue
        if lookup(conn, term, item.get("quote", "")):
            got.append(term)
            today_lookups += 1
            today_terms += 1
        else:
            skipped.append({"term": term, "why": "没学会"})
    return {"learned": got, "skipped": skipped}


def observe(conn: sqlite3.Connection, lines: list[dict[str, str]], reply: str = "") -> dict[str, Any]:
    """一次「复盘」：判词 → 学词。同步调用（HTTP 接口把它丢到后台线程）。"""
    terms = judge(lines, reply)
    if not terms:
        return {"judged": [], "learned": [], "skipped": []}
    res = learn(conn, terms)
    return {"judged": [t["term"] for t in terms], **res}


def pick(conn: sqlite3.Connection, limit: int = 8, days: int = 2) -> list[dict[str, str]]:
    """只挑词、不学（给 --dry 看眼光用）。"""
    ensure(conn)
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    rows = conn.execute(
        "SELECT sender_name, text FROM messages WHERE ts >= ? AND length(text) BETWEEN 2 AND 300"
        " ORDER BY id DESC LIMIT 400",
        (since,),
    ).fetchall()
    if not rows:
        # 没有新消息（群静了、或者同步没跑）时不要什么都不做：退回「最近 200 条」，
        # 至少还能补上次没挖完的词。学过的词会被 learn() 跳过，重复跑不花钱。
        rows = conn.execute(
            "SELECT sender_name, text FROM messages WHERE length(text) BETWEEN 2 AND 300"
            " ORDER BY id DESC LIMIT 200"
        ).fetchall()
    if not rows:
        return []
    convo = "\n".join(f"{r['sender_name']}：{r['text']}" for r in reversed(rows))
    try:
        raw = _llm(_MINE_SYSTEM, "群聊：\n" + convo[-6000:], temperature=0.3, timeout=60)
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in (data.get("terms") or [])[:limit]:
        term = clean_term(str(item.get("term") or ""))
        if _usable(term):
            out.append({"term": term, "quote": str(item.get("quote") or "")[:MAX_CONTEXT_LEN]})
    return out


def mine(conn: sqlite3.Connection, limit: int = 8, days: int = 2) -> dict[str, Any]:
    """每日挖词：从最近的群聊里找「群内专属说法」，不用等它被问到。

    和 observe 的分工：observe 走「我没看懂」这条线，很准但漏得也多（没人问就学不到）；
    mine 走「这个群老说一些怪词」这条线，主动补。两条线共用同一个词库和额度。
    """
    picked = pick(conn, limit=limit, days=days)
    res = learn(conn, picked)
    return {"picked": [p["term"] for p in picked], **res}


# --------------------------------------------------------------------- 后台

_LOCK = threading.Lock()
_PENDING: list[tuple[list[dict[str, str]], str]] = []
_STATE: dict[str, Any] = {"running": False, "queued": 0, "last": None, "last_ok": None}


def _worker() -> None:
    """一条后台线程把排队的复盘跑完。判词 + 查网一个词要几秒，不能占着请求。"""
    from . import db

    while True:
        with _LOCK:
            if not _PENDING:
                _STATE["running"] = False
                _STATE["queued"] = 0
                return
            lines, reply = _PENDING.pop(0)
            _STATE["queued"] = len(_PENDING)
        c = db.connect()
        try:
            res = observe(c, lines, reply)
            _STATE["last"] = {"ts": _now(), **res}
            if res.get("learned"):
                _STATE["last_ok"] = res["learned"]
        except Exception as exc:  # noqa: BLE001
            _STATE["last"] = {"ts": _now(), "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def observe_async(lines: list[dict[str, str]], reply: str = "") -> dict[str, Any]:
    """接口用：立刻返回，学习在后台线程里跑（判词要 2 秒、查网要 3 秒，不能挡回复）。

    排队上限 3 条：群聊刷屏时不该无限堆任务，直接拒掉比悄悄变慢好。
    """
    with _LOCK:
        if len(_PENDING) >= 3:
            return {"accepted": False, "reason": "排队的复盘太多了"}
        _PENDING.append((lines, reply))
        _STATE["queued"] = len(_PENDING)
        if _STATE["running"]:
            return {"accepted": True, "queued": True}
        _STATE["running"] = True
    threading.Thread(target=_worker, daemon=True).start()
    return {"accepted": True, "queued": False}


def state() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE)
