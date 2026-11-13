"""事实记忆（v0.14）：从群聊里抽「谁 / 什么 是什么样」，人审过才生效。

为什么这一层要「人审」而纠错层不用：

- 纠错是**用户主动说的**，错了也是他自己的错，责任清楚；
- 抽取是**模型猜的**。一条猜错的事实（「147 爱吃鱼片」其实只是他问过一次）
  会被当成「群里的常识」反复引用，还会被检索给别的回答当依据——
  一次答错只错一句，一条脏事实会错一片。

所以这里的分工是：**模型负责出草稿，人负责点头**。
`status` 三态：`pending`（待审，检索不到）→ `approved`（生效）/ `rejected`（丢掉）。
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from .text import to_bigrams

MAX_SUBJECT = 40
MAX_CLAIM = 200
MAX_QUOTE = 240
MAX_MSG_IDS = 200
SIM_DUP = 0.70  # 同一 subject 下，claim 二元组 Jaccard 超过这个值就当重复


def _grams(text: str) -> set[str]:
    """to_bigrams 返回的是空格拼起来的串（给 FTS5 用的），这里要集合。"""
    return set(t for t in to_bigrams(text).split(" ") if t)


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _key(subject: str, claim: str) -> str:
    return (subject or "").strip() + "|" + (claim or "").strip()


def add_candidates(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> dict[str, int]:
    """把抽取出来的草稿写进库（status=pending）。

    去重分两层：
    1. `subject|claim` 完全相同 —— 换了批次的重复；
    2. 同一 subject 下，claim 的二元组 Jaccard ≥ SIM_DUP ——
       「口头禅是杂口杂口和扎口」/「常用扎口扎口作语气词」这种同一件事的两种说法，
       不去重的话待审列表会被同一件事刷屏，审的人第一次就会烦，然后就再也不审了。
    """
    have: dict[str, list[str]] = {}
    for r in conn.execute("SELECT subject, claim FROM facts"):
        have.setdefault(r["subject"], []).append(r["claim"])
    added = dup = bad = 0
    for it in items or []:
        if not isinstance(it, dict):
            bad += 1
            continue
        subject = str(it.get("subject") or "").strip()[:MAX_SUBJECT]
        claim = str(it.get("claim") or "").strip()[:MAX_CLAIM]
        if not subject or not claim:
            bad += 1
            continue
        if claim in have.get(subject, []):
            dup += 1
            continue
        grams = _grams(claim)
        if any(_similar(grams, _grams(c)) >= SIM_DUP for c in have.get(subject, [])):
            dup += 1
            continue
        ids = it.get("msg_ids")
        if isinstance(ids, (list, tuple)):
            ids = ",".join(str(x) for x in ids)
        conn.execute(
            "INSERT INTO facts (ts, subject, claim, quote, msg_ids) VALUES (?, ?, ?, ?, ?)",
            (
                _now(),
                subject,
                claim,
                str(it.get("quote") or "").strip()[:MAX_QUOTE],
                str(ids or "").strip()[:MAX_MSG_IDS],
            ),
        )
        have.setdefault(subject, []).append(claim)
        added += 1
    conn.commit()
    return {"added": added, "dup": dup, "bad": bad}


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """重建事实的关键词索引。只索引 approved——pending 还没人看过，不该被检索到。"""
    conn.execute("DELETE FROM facts_fts")
    rows = conn.execute("SELECT id, subject, claim FROM facts WHERE status = 'approved'").fetchall()
    for r in rows:
        text = f"{r['subject']} {r['claim']}"
        for bg in to_bigrams(text).split():
            if not bg.strip():
                continue
            conn.execute(
                "INSERT INTO facts_fts (bigram, fact_id) VALUES (?, ?)", (bg, r["id"])
            )
    conn.commit()
    return len(rows)


def decide(conn: sqlite3.Connection, fid: int, status: str) -> int:
    """审一条：approved 生效 / rejected 丢掉 / pending 退回待审。"""
    if status not in ("approved", "rejected", "pending"):
        raise ValueError("status 只能是 approved / rejected / pending")
    cur = conn.execute(
        "UPDATE facts SET status = ?, decided_at = ? WHERE id = ?", (status, _now(), int(fid))
    )
    conn.commit()
    if cur.rowcount:
        rebuild_fts(conn)
    return int(cur.rowcount or 0)


def delete(conn: sqlite3.Connection, fid: int) -> int:
    cur = conn.execute("DELETE FROM facts WHERE id = ?", (int(fid),))
    conn.commit()
    if cur.rowcount:
        rebuild_fts(conn)
    return int(cur.rowcount or 0)


def search(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """在 approved 的事实里找。先 FTS（AND 再 OR 回退），再 LIKE 兜底。

    没有向量那一路——事实条目短、且是人工点头过的东西，
    关键词命中已经够准；为几十条事实算向量不划算。
    """
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit or 5), 20))
    bgs = to_bigrams(q).split()      # to_bigrams 返回空格串，split 才是 token 列表
    bgs = [b for b in bgs if b.strip()]
    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    for expr in (" AND ".join(bgs), " OR ".join(bgs)):
        if not expr:
            break
        try:
            rows = conn.execute(
                "SELECT f.id, f.subject, f.claim, f.quote, f.msg_ids,"
                "       bm25(facts_fts) AS score"
                "  FROM facts_fts JOIN facts f ON f.id = facts_fts.fact_id"
                " WHERE facts_fts MATCH ? AND f.status = 'approved'"
                " ORDER BY score LIMIT ?",
                (expr, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(
                {
                    "id": r["id"],
                    "subject": r["subject"],
                    "claim": r["claim"],
                    "quote": r["quote"] or "",
                    "msg_ids": r["msg_ids"] or "",
                    "score": round(-float(r["score"] or 0), 4),
                }
            )
            if len(out) >= limit:
                break
        if out:
            break

    if not out and len(q) >= 2:
        like = f"%{q[:20]}%"
        for r in conn.execute(
            "SELECT id, subject, claim, quote, msg_ids FROM facts"
            " WHERE status = 'approved' AND (claim LIKE ? OR subject LIKE ?)"
            " ORDER BY id DESC LIMIT ?",
            (like, like, limit),
        ):
            out.append(
                {
                    "id": r["id"],
                    "subject": r["subject"],
                    "claim": r["claim"],
                    "quote": r["quote"] or "",
                    "msg_ids": r["msg_ids"] or "",
                    "score": 0.0,
                }
            )
    return out


def list_rows(conn: sqlite3.Connection, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
    if status not in ("pending", "approved", "rejected", "all"):
        status = "pending"
    limit = max(1, min(int(limit or 100), 300))
    if status == "all":
        rows = conn.execute(
            "SELECT * FROM facts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM facts WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def bump(conn: sqlite3.Connection, ids: list[int]) -> None:
    for fid in ids:
        conn.execute("UPDATE facts SET hits = hits + 1 WHERE id = ?", (int(fid),))
    conn.commit()


# ---------------------------------------------------------------- 自动点头（v0.19）
#
# 为什么要有这一步：待审列表堆到 200 多条时，人会直接放弃——然后就再也不审了，
# 于是「人工把关」这条防线等于没有。真正的出路不是催人点，而是**把可机器验证的那部分自动掉**。
#
# 唯一可信的机器信号是「原话真的在它引用的那条消息里」：
#   - 抽取的模型会编，但「编出来的原话」在 messages 表里查不到，这条信号抓得住它；
#   - 这不是语义判断，是字符串比对，不会引入新的主观性。
# 所以自动点头的硬条件是 grounded（原话能在 msg_ids 里找到），再加一点身份/长度下限。
# 只要 grounded，即使 claim 的表达方式不完美，它「不是凭空编的」这件事是确定的。
# 自动点头的都打 auto=1，`--undo-auto` 可以一键全退回 pending。

AUTO_MIN_SCORE = 11
AUTO_MAX_JUNK = 3
_BAD_SUBJECTS = {"bot", "机器人", "助手", "群助手"}
# 结论里出现这些词的，多半是「机器人怎么怎么样」这类元信息，不是群友的事实
_META_WORDS = ("bot", "机器人", "助手", "astrbot", "@", "指令", "插件")


def _senders(conn: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT sender_name FROM messages "
                "WHERE sender_name IS NOT NULL AND sender_name != ''"
            )
        }
    except sqlite3.Error:
        return set()


def _parse_ids(raw: Any) -> list[int]:
    out: list[int] = []
    for part in str(raw or "").replace("，", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out[:20]


def _evidence(conn: sqlite3.Connection, quote: str, msg_ids: Any) -> tuple[bool, str]:
    """返回 (原话是否真在引用消息里, 引用消息的原文拼起来)。

    「原话能查到」只证明它没编，所以这里顺手把原文带出来，
    后面还要看「结论到底有没有原话支撑」——两句「我要当」撑不起「想当最牛逼的动漫人物」。
    """
    ids = _parse_ids(msg_ids)
    needle = str(quote or "").strip().strip("「」\"“”'‘’ ")[:12]
    if not ids:
        return False, ""
    marks = ",".join("?" * len(ids))
    try:
        rows = conn.execute(f"SELECT text FROM messages WHERE id IN ({marks})", ids).fetchall()
    except sqlite3.Error:
        return False, ""
    pool = " ".join((r[0] or "") for r in rows)
    if not needle:
        return False, pool
    return (needle in pool), pool


def grounded(conn: sqlite3.Connection, quote: str, msg_ids: Any) -> bool:
    return _evidence(conn, quote, msg_ids)[0]


def _support_ratio(claim: str, quote: str, pool: str) -> float:
    """结论的二元组有多少能在「原话 + 引用消息」里找到。"""
    cg = _grams(claim)
    if not cg:
        return 0.0
    eg = _grams(quote) | _grams(pool)
    return len(cg & eg) / len(cg)


def score(conn: sqlite3.Connection, row: Any, senders: set[str] | None = None) -> tuple[int, list[str]]:
    """给一条待审事实打分。分数只用来分流，不用来排序：

    - `≥ AUTO_MIN_SCORE`（9）→ 自动点头（事实清楚、主语是真人、结论不带元信息）
    - `≤ AUTO_MAX_JUNK`（3）→ 自动拒绝（原话查不到 / 主语是机器人 / 结论只说了「相关」）
    - 中间 → 留给人看

    踩过两个坑，都是**打分维度选错了**：
    1. 光看「原话能在引用消息里查到」→ 140 条过了 138 条。抽取本来就是抄原话，
       能查到只证明它没编，不证明这条有用。
    2. 又加「原话至少 8 个字」→ 把「147｜想吃鱼片」这种真事实也毙了。
       原话短不等于事实假，短的原话照样能撑起一条结论，所以长度只能做加减分，不能做门槛。
    """
    senders = _senders(conn) if senders is None else senders
    subject = str(row["subject"] or "").strip()
    claim = str(row["claim"] or "").strip()
    quote = str(row["quote"] or "").strip()
    why: list[str] = []

    ok, pool = _evidence(conn, quote, row["msg_ids"])
    if not ok:
        return 0, ["原话在引用消息里查不到（可能是编的）"]

    if subject.lower() in _BAD_SUBJECTS:
        return 2, ["原话可查，但主语是机器人自己"]

    low = claim.lower()
    if any(w in low for w in _META_WORDS):
        return 3, ["原话可查，但结论在讲机器人/指令"]
    if any(w in claim for w in ("相关", "提到", "说过")) and len(claim) <= 12:
        return 3, ["原话可查，但结论只说了「相关/提到」"]

    if subject not in senders:
        return 5, ["原话可查，但主语不是群里说过话的人"]

    ratio = _support_ratio(claim, quote, pool)
    if ratio < 0.5:
        # 门槛定在 0.5 是量出来的：定成 0.35 时，「还来污染管理员了」会被读成
        # 「是群管理员」、「ig」会被读成「看好IG」——这类推断留着比没有更糟。
        return 6, [f"原话撑不起这条结论（支撑度 {ratio:.0%}），留给人看"]

    s = 9
    why.append("原话可查")
    why.append("主语是群里说过话的人")
    if ratio >= 0.6:
        s += 1
        why.append(f"结论有原话支撑（{ratio:.0%}）")
    if 3 <= len(claim) <= 40:
        s += 1
    else:
        s -= 2
        why.append("结论长度反常")
    return s, why

def auto_approve(
    conn: sqlite3.Connection,
    min_score: int = AUTO_MIN_SCORE,
    dry: bool = True,
    limit: int = 0,
) -> dict[str, Any]:
    """扫 pending，够分的自动 approved（打 auto=1）。dry=True 只报告不写库。"""
    rows = conn.execute(
        "SELECT id, subject, claim, quote, msg_ids FROM facts WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    if limit and limit > 0:
        rows = rows[:limit]
    senders = _senders(conn)
    picked: list[tuple[int, int, list[str]]] = []
    # 同一件事常被抽出两次（「设置了每日重置使鱼忘记扎口」/「设置了每日重置导致鱼忘了扎口」），
    # 自动点头要顺手去重，否则事实库里会塞满近义条目、把检索位挤掉。
    seen: list[tuple[str, set[str]]] = []
    try:
        for r in conn.execute(
            "SELECT subject, claim FROM facts WHERE status = 'approved'"
        ):
            seen.append((str(r["subject"]), _grams(str(r["claim"]))))
    except sqlite3.Error:
        pass
    for r in rows:
        sc, why = score(conn, r, senders)
        if sc < min_score:
            continue
        subj, cg = str(r["subject"]), _grams(str(r["claim"]))
        if any(s == subj and _similar(g, cg) >= 0.75 for s, g in seen):
            continue
        seen.append((subj, cg))
        picked.append((int(r["id"]), sc, why))

    if not dry and picked:
        marks = ",".join("?" * len(picked))
        conn.execute(
            f"UPDATE facts SET status='approved', auto=1, decided_at=? WHERE id IN ({marks})",
            (_now(), *[p[0] for p in picked]),
        )
        conn.commit()
        rebuild_fts(conn)

    return {
        "scanned": len(rows),
        "picked": len(picked),
        "applied": 0 if dry else len(picked),
        "detail": [
            {"id": i, "score": sc, "why": why} for i, sc, why in picked[:20]
        ],
    }


def auto_reject(
    conn: sqlite3.Connection,
    max_score: int = AUTO_MAX_JUNK,
    dry: bool = True,
    limit: int = 0,
) -> dict[str, Any]:
    """扫 pending，明显是垃圾的自动 rejected（打 auto=2）。

    只吃「几乎肯定没用」的：原话查不到 / 原话太短 / 主语是机器人且结论在讲元信息。
    这些留着只会把待审列表撑大，人越看越不想看。
    """
    rows = conn.execute(
        "SELECT id, subject, claim, quote, msg_ids FROM facts WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    if limit and limit > 0:
        rows = rows[:limit]
    senders = _senders(conn)
    picked: list[tuple[int, int, list[str]]] = []
    for r in rows:
        sc, why = score(conn, r, senders)
        if sc <= max_score:
            picked.append((int(r["id"]), sc, why))

    if not dry and picked:
        marks = ",".join("?" * len(picked))
        conn.execute(
            f"UPDATE facts SET status='rejected', auto=2, decided_at=? WHERE id IN ({marks})",
            (_now(), *[p[0] for p in picked]),
        )
        conn.commit()

    return {
        "scanned": len(rows),
        "picked": len(picked),
        "applied": 0 if dry else len(picked),
        "detail": [{"id": i, "score": sc, "why": why} for i, sc, why in picked[:20]],
    }


def undo_auto(conn: sqlite3.Connection) -> int:
    """把所有自动点头/自动拒绝的退回待审（一键撤销）。人工点过的（auto=0）不动。"""
    cur = conn.execute(
        "UPDATE facts SET status='pending', auto=0, decided_at=? WHERE auto != 0", (_now(),)
    )
    conn.commit()
    if cur.rowcount:
        rebuild_fts(conn)
    return int(cur.rowcount or 0)


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    try:
        row = conn.execute(
            "SELECT"
            " COALESCE(SUM(status = 'pending'), 0) pending,"
            " COALESCE(SUM(status = 'approved'), 0) approved,"
            " COALESCE(SUM(status = 'rejected'), 0) rejected,"
            " COALESCE(SUM(hits), 0) hits"
            " FROM facts"
        ).fetchone()
        return {k: int(row[k] or 0) for k in ("pending", "approved", "rejected", "hits")}
    except sqlite3.Error:
        return {"pending": 0, "approved": 0, "rejected": 0, "hits": 0}
