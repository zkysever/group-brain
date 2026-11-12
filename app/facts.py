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
