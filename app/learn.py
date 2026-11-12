"""纠错记忆（v0.13）：它答错过一次的地方，下次按你说的答。

为什么「学习」从纠错开始，而不是先搞「自动总结知识」：

1. **信号是用户主动给的**。自动抽取（从群聊里猜「谁喜欢什么」）会自己编，
   而「不对，应该是 X」是确定的事实——先把确定的东西用起来，再谈猜的。
2. **一次纠正终身生效，还能算命中率**。有多少条纠正被真正用上（`hits` 列），
   是「它到底学没学」唯一诚实的指标。
3. **每条纠错顺手就是一条评测题**（问题 + 正确答案）。学习和评测共用同一份数据，
   不会出现「评测集是拍脑袋写的、学习是从真实对话里学的」两套东西对不上。

做一个刻意的取舍：**命中就原样答，不让模型改写**。
因为纠正的语义常常很窄（「白系 AP 是《舞萌》里的一首歌」），
让模型"用自己的话说一遍"，它会顺手补一堆自己编的细节——那就等于没纠成。
"""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from typing import Any

from .text import to_bigrams

MAX_RIGHT = 800       # 单条正确答案最多存这么长，防手滑贴一篇
MAX_ROWS = 500        # 只留最近这么多条，老的自然淘汰
MIN_FUZZY = 4         # 少于 4 个字不做「包含」匹配，否则「你好」会吃遍全站

_SPLIT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def norm(text: str) -> str:
    """归一化：全角转半角、小写、去掉标点和空白。

    「帮我代一个白系AP」和「帮我代一个白系 ap？」要能被认成同一句，
    但不必聪明到理解同义词——那是检索该干的事，不是这里。
    """
    t = unicodedata.normalize("NFKC", text or "").lower()
    return _SPLIT.sub("", t)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def record(
    conn: sqlite3.Connection,
    question: str,
    right: str,
    wrong: str = "",
    source: str = "web",
    session_id: str = "",
) -> int:
    """记一条纠正。同一个问题（归一化后相同）再教一次 = 覆盖，不叠加。

    返回这条的 id。
    """
    q = (question or "").strip()
    r = (right or "").strip()
    if not q or not r:
        raise ValueError("问题和正确答案都不能为空")
    n = norm(q)
    if not n:
        raise ValueError("这句话里没有任何可用来匹配的字")
    r = r[:MAX_RIGHT]

    row = conn.execute("SELECT id FROM corrections WHERE norm = ?", (n,)).fetchone()
    if row:
        conn.execute(
            "UPDATE corrections SET ts = ?, question = ?, wrong = ?, right = ?, source = ? WHERE id = ?",
            (_now(), q, (wrong or "").strip()[:MAX_RIGHT], r, source, row["id"]),
        )
        cid = int(row["id"])
    else:
        cur = conn.execute(
            "INSERT INTO corrections (ts, question, norm, wrong, right, source, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), q, n, (wrong or "").strip()[:MAX_RIGHT], r, source, session_id),
        )
        cid = int(cur.lastrowid or 0)

    # 只留最近 MAX_ROWS 条：学习表无限长最后会变成垃圾场
    conn.execute(
        "DELETE FROM corrections WHERE id NOT IN"
        " (SELECT id FROM corrections ORDER BY id DESC LIMIT ?)",
        (MAX_ROWS,),
    )
    conn.commit()
    return cid


def find(conn: sqlite3.Connection, question: str) -> dict[str, Any] | None:
    """查这句话有没有被纠正过。三层，从严到松：

    1. 归一化后完全一样 —— 最可信；
    2. 一问包含另一问（都至少 4 个字）—— 处理「帮我代一个白系ap」vs「帮我代一个白系ap啊」；
    3. 二元组互相包含 —— 处理中间多插了几个字的情况。

    都没命中就返回 None，老老实实走模型。宁可漏，不要错杀：
    一条错的纠错会让它在该答对的地方答错，比不学更糟。
    """
    q = (question or "").strip()
    if not q:
        return None
    n = norm(q)
    if not n:
        return None

    row = conn.execute(
        "SELECT * FROM corrections WHERE norm = ? ORDER BY id DESC LIMIT 1", (n,)
    ).fetchone()

    if row is None and len(n) >= MIN_FUZZY:
        # 包含：取最长的那个 norm，避免短句把长句吃掉
        row = conn.execute(
            "SELECT * FROM corrections WHERE length(norm) >= ?"
            " AND (instr(?, norm) > 0 OR instr(norm, ?) > 0)"
            " ORDER BY length(norm) DESC, id DESC LIMIT 1",
            (MIN_FUZZY, n, n),
        ).fetchone()

    if row is None and len(n) >= MIN_FUZZY:
        qb = set(to_bigrams(n))
        if qb:
            # 二元组全包含：候选不会太多，直接在 Python 里判，别写复杂的 SQL
            for cand in conn.execute(
                "SELECT * FROM corrections WHERE length(norm) >= ? ORDER BY id DESC LIMIT 100",
                (MIN_FUZZY,),
            ):
                cb = set(to_bigrams(cand["norm"]))
                if cb and (qb <= cb or cb <= qb):
                    row = cand
                    break

    if row is None:
        return None

    conn.execute("UPDATE corrections SET hits = hits + 1 WHERE id = ?", (row["id"],))
    conn.commit()
    return dict(row)


def all_rows(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, ts, question, right, wrong, source, hits FROM corrections"
        " ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 200)),),
    ).fetchall()
    return [dict(r) for r in rows]


def delete(conn: sqlite3.Connection, cid: int) -> int:
    cur = conn.execute("DELETE FROM corrections WHERE id = ?", (int(cid),))
    conn.commit()
    return int(cur.rowcount or 0)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(hits), 0) hits FROM corrections"
        ).fetchone()
        return {"total": int(row["n"]), "hits": int(row["hits"])}
    except sqlite3.Error:
        return {"total": 0, "hits": 0}
