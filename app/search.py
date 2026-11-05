"""检索层：FTS5 关键词检索 + RRF 融合（向量检索留到 v0.4）。"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from .text import to_bigrams


# ---------------------------------------------------------------- FTS 检索

def _fts_query(tokens: list[str], joiner: str) -> str:
    """每个 token 用双引号包起来，防止 - + : 这类字符把查询语法搞崩。"""
    safe = [t.replace('"', "") for t in tokens if t]
    return joiner.join('"' + t + '"' for t in safe)


def _search_fts(
    conn: sqlite3.Connection,
    table: str,
    id_col: str,
    query: str,
    limit: int,
) -> list[tuple[int, float]]:
    """先 AND 求精确，命中太少再 OR 放宽。返回 [(原始id, 正分数)]。"""
    tokens = to_bigrams(query).split()
    if not tokens:
        return []

    for joiner in (" ", " OR "):
        expr = _fts_query(tokens, joiner)
        if not expr:
            continue
        try:
            rows = conn.execute(
                f"SELECT {id_col} AS oid, bm25({table}) AS rank "
                f"FROM {table} WHERE {table} MATCH ? "
                f"ORDER BY rank LIMIT ?",
                (expr, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if rows:
            # bm25 是负数，越负越相关；取反变成正分
            return [(int(r["oid"]), -float(r["rank"])) for r in rows]
    return []


def _like_fallback(
    conn: sqlite3.Connection,
    table: str,
    text_col: str,
    query: str,
    limit: int,
) -> list[tuple[int, float]]:
    """单字查询（如"鱼"）bigram 匹配不到，退化成 LIKE。"""
    q = query.strip()
    if not q:
        return []
    rows = conn.execute(
        f"SELECT id, {text_col} AS t FROM {table} "
        f"WHERE {text_col} LIKE ? ORDER BY id DESC LIMIT ?",
        (f"%{q}%", limit),
    ).fetchall()
    return [(int(r["id"]), 0.5) for r in rows]


# ---------------------------------------------------------------- RRF 融合

def rrf_fuse(lists: list[list[tuple[int, float]]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：多路召回只比名次，不比原始分数。

    好处：bm25 的分数和向量余弦完全不可比，硬加权调参数会调死人。
    """
    score: dict[int, float] = {}
    for ranked in lists:
        for rank, (oid, _raw) in enumerate(ranked, start=1):
            score[oid] = score.get(oid, 0.0) + 1.0 / (k + rank)
    return sorted(score.items(), key=lambda kv: kv[1], reverse=True)


# ---------------------------------------------------------------- 对外接口

def search_messages(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict[str, Any]]:
    hits = _search_fts(conn, "messages_fts", "msg_id", query, limit * 3)
    if not hits:
        hits = _like_fallback(conn, "messages", "text", query, limit * 3)
    if not hits:
        return []
    hits = hits[:limit]
    ids = [h[0] for h in hits]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, ts, session, sender_name, kind, text FROM messages WHERE id IN ({marks})",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    out = []
    for oid, sc in hits:
        r = by_id.get(oid)
        if not r:
            continue
        out.append(
            {
                "type": "message",
                "id": r["id"],
                "score": round(sc, 6),
                "text": r["text"],
                "sender_name": r["sender_name"] or "",
                "kind": r["kind"] or "",
                "ts": r["ts"] or "",
                "session": r["session"] or "",
            }
        )
    return out


def search_memes(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict[str, Any]]:
    hits = _search_fts(conn, "memes_fts", "meme_id", query, limit * 3)
    if not hits:
        # 表情包的 caption / keywords 分开两份，LIKE 兜底要查两列
        q = query.strip()
        rows = []
        if q:
            rows = conn.execute(
                "SELECT id FROM memes WHERE caption LIKE ? OR keywords LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                (f"%{q}%", f"%{q}%", limit * 3),
            ).fetchall()
        hits = [(int(r["id"]), 0.5) for r in rows]
    if not hits:
        return []
    hits = hits[:limit]
    ids = [h[0] for h in hits]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, pack, tag, caption, keywords, path, file_name FROM memes WHERE id IN ({marks})",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    out = []
    for oid, sc in hits:
        r = by_id.get(oid)
        if not r:
            continue
        out.append(
            {
                "type": "meme",
                "id": r["id"],
                "score": round(sc, 6),
                "text": r["caption"] or "",
                "keywords": r["keywords"] or "",
                "tag": r["tag"] or "",
                "pack": r["pack"] or "",
                "url": f"/api/meme/{r['id']}",
            }
        )
    return out


def search_all(conn: sqlite3.Connection, query: str, kind: str = "all", limit: int = 10) -> dict[str, Any]:
    t0 = time.perf_counter()
    kind = (kind or "all").lower()

    msg_hits = search_messages(conn, query, limit) if kind in ("all", "message", "messages") else []
    meme_hits = search_memes(conn, query, limit) if kind in ("all", "meme", "memes") else []

    if kind == "all" and msg_hits and meme_hits:
        fused = rrf_fuse(
            [
                [(h["id"], h["score"]) for h in msg_hits],
                [(h["id"] + 10**7, h["score"]) for h in meme_hits],
            ]
        )
        pool = {h["id"]: h for h in msg_hits}
        pool.update({h["id"] + 10**7: h for h in meme_hits})
        hits = [pool[oid] for oid, _ in fused][:limit]
    else:
        hits = (msg_hits + meme_hits)[:limit]

    took = (time.perf_counter() - t0) * 1000
    return {
        "query": query,
        "kind": kind,
        "took_ms": round(took, 2),
        "count": len(hits),
        "hits": hits,
    }


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(sql: str, *args) -> int:
        row = conn.execute(sql, args).fetchone()
        return int(row[0]) if row else 0

    tags = [
        {"tag": r["tag"], "n": r["n"]}
        for r in conn.execute(
            "SELECT tag, COUNT(*) AS n FROM memes GROUP BY tag ORDER BY n DESC"
        ).fetchall()
    ]
    senders = [
        {"name": r["sender_name"], "n": r["n"]}
        for r in conn.execute(
            "SELECT sender_name, COUNT(*) AS n FROM messages "
            "GROUP BY sender_name ORDER BY n DESC LIMIT 8"
        ).fetchall()
    ]
    span = conn.execute(
        "SELECT MIN(ts) AS a, MAX(ts) AS b FROM messages WHERE ts IS NOT NULL AND ts != ''"
    ).fetchone()
    return {
        "messages": one("SELECT COUNT(*) FROM messages"),
        "memes": one("SELECT COUNT(*) FROM memes"),
        "vectors": one("SELECT COUNT(*) FROM vectors"),
        "tags": tags,
        "senders": senders,
        "ts_from": span["a"] if span else "",
        "ts_to": span["b"] if span else "",
    }
