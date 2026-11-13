"""A2 的对话记忆：存在服务端，不再只活在浏览器内存里。

为什么要有这个模块（2026-11-12 改的）：

  v0.9 那会儿，「刚才那句」的上下文是前端 `HISTORY` 数组拼成 `history=` 参数传上来的。
  能解决指代，但有三个硬伤：
    1. 刷新即失忆——关掉页面再打开，它就不认识你了；
    2. 换设备即失忆——手机问一半，电脑接不上；
    3. 客户端能伪造——虽然是白名单过滤过的，但它终究是「客户端说的上文」，不是「服务端记下的事实」。

  现在改成：一个浏览器一个 `session_id`（cookie 里），每轮问答落库，下一次提问时从库里取最近 N 轮。
  客户端不再需要（也不应该）提供上下文。

两条设计约束：

  1. **会话不会无限长**。`KEEP_TURNS` 是每个 session 保留的轮数上限，超了从最老的删。
     这不是抠门，是因为无限增长的表迟早会把查询变慢、把备份变大；而且要「记住一切」的正确做法
     是把原文留在 messages 表里检索，不是把所有历史都塞进 prompt。
  2. **只存问答，不存工具轨迹**。工具调用的中间结果每条几百字，带进下一轮纯属浪费——
     需要事实的时候它会再调一次工具，那才是可靠的信息来源。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

# 每个 session 最多保留多少「轮」（一问一答算一轮）
KEEP_TURNS = 200
# 单条历史在库里最多存多少字（防止一次粘贴几万字把库撑爆）
STORE_CHARS = 2000

# 「口令即身份」（v0.18）：有这个口令的人共用一段记忆。
# 为什么改：cookie 方案的记忆是「这台浏览器的」，手机问一半电脑接不上——
# 而这套东西只有一个使用者（网页后面挂着一道口令门），那就没必要按浏览器分会话。
# 想退回旧行为（一浏览器一段记忆）：把 AGENT_SHARED_MEMORY=0 写进 .env。
SHARED_SESSION = "main"


def new_session() -> str:
    return uuid.uuid4().hex


def adopt_into(conn: sqlite3.Connection, target: str = SHARED_SESSION) -> int:
    """把「最近在用的那个旧 cookie 会话」并进共享会话，返回并进来几条。

    只在 target 还是一张白纸时做一次。不这么做的话，用户升级完会发现
    「我之前的对话它忘了」——明明库里还在，只是换了 session_id 查不到。
    """
    n = conn.execute(
        "SELECT COUNT(*) FROM agent_turns WHERE session_id=?", (target,)
    ).fetchone()[0]
    if n:
        return 0
    row = conn.execute(
        "SELECT session_id, COUNT(*) AS c FROM agent_turns "
        "WHERE session_id != ? GROUP BY session_id ORDER BY MAX(id) DESC LIMIT 1",
        (target,),
    ).fetchone()
    if not row:
        return 0
    cur = conn.execute(
        "UPDATE agent_turns SET session_id=? WHERE session_id=?",
        (target, row["session_id"]),
    )
    conn.commit()
    return cur.rowcount


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def append(conn: sqlite3.Connection, session_id: str, role: str, content: str) -> None:
    """记一句。role 白名单在这里把住，坏的直接扔掉而不是写进去。"""
    if role not in ("user", "assistant"):
        return
    text = (content or "").strip()
    if not text:
        return
    conn.execute(
        "INSERT INTO agent_turns(session_id, ts, role, content) VALUES(?,?,?,?)",
        (session_id, _now(), role, text[:STORE_CHARS]),
    )
    conn.commit()


def recent(conn: sqlite3.Connection, session_id: str, turns: int) -> list[dict[str, str]]:
    """取最近 `turns` 轮，按时间正序返回，直接能塞进 messages。"""
    if not session_id or turns <= 0:
        return []
    rows = conn.execute(
        "SELECT role, content FROM agent_turns WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, turns * 2),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def turn_count(conn: sqlite3.Connection, session_id: str) -> int:
    """这个 session 一共聊了几轮（一问一答算一轮）。"""
    n = conn.execute(
        "SELECT COUNT(*) FROM agent_turns WHERE session_id=?", (session_id,)
    ).fetchone()[0]
    return int(n) // 2


def forget(conn: sqlite3.Connection, session_id: str) -> int:
    """清空一个 session 的记忆，返回删了几条。"""
    cur = conn.execute("DELETE FROM agent_turns WHERE session_id=?", (session_id,))
    conn.commit()
    return cur.rowcount


def trim(conn: sqlite3.Connection, session_id: str, keep_turns: int = KEEP_TURNS) -> int:
    """把超出保留上限的老问答删掉，返回删了几条。"""
    keep = keep_turns * 2
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM agent_turns WHERE session_id=? ORDER BY id DESC LIMIT -1 OFFSET ?",
            (session_id, keep),
        ).fetchall()
    ]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    cur = conn.execute(f"DELETE FROM agent_turns WHERE id IN ({marks})", ids)
    conn.commit()
    return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict:
    """给管理面板看的：多少个会话、多少条记忆。"""
    row = conn.execute(
        "SELECT COUNT(DISTINCT session_id) AS sessions, COUNT(*) AS turns FROM agent_turns"
    ).fetchone()
    return {"sessions": int(row["sessions"] or 0), "turns": int(row["turns"] or 0)}
