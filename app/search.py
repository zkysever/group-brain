"""检索层：FTS5 关键词 + bge-m3 向量，两路用 RRF 融合。

为什么非得融合：bm25 吃字面（"开盒"搜不到"开个盒"），向量吃语义（但会把
"帮我开盒"和"开盒我不干"判成同一件事）。两边错的方向不一样，只留一路就一直错，
融合才有机会救回来。RRF 只比名次不比分数，省掉调权重这个无底洞。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import config as cfg
from . import vectors
from .text import to_bigrams

MODES = ("auto", "keyword", "vector", "hybrid")

# 消息 id 和表情包 id 会撞车，融合时给表情包加一个大偏移，取回来再减掉
MEME_OFF = 10**7


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


def _kw_messages(conn, query: str, limit: int) -> list[tuple[int, float]]:
    hits = _search_fts(conn, "messages_fts", "msg_id", query, limit)
    if not hits:
        hits = _like_fallback(conn, "messages", "text", query, limit)
    return hits


def _kw_memes(conn, query: str, limit: int) -> list[tuple[int, float]]:
    hits = _search_fts(conn, "memes_fts", "meme_id", query, limit)
    if hits:
        return hits
    q = query.strip()
    if not q:
        return []
    rows = conn.execute(
        "SELECT id FROM memes WHERE caption LIKE ? OR keywords LIKE ? "
        "ORDER BY id DESC LIMIT ?",
        (f"%{q}%", f"%{q}%", limit),
    ).fetchall()
    return [(int(r["id"]), 0.5) for r in rows]


# ---------------------------------------------------------------- RRF 融合

def rrf_fuse(
    lists: list[list[tuple[int, float]]],
    k: int = 60,
    weights: list[float] | None = None,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：多路召回只比名次，不比原始分数。

    好处：bm25 的分数和向量余弦完全不可比，硬加权调参数会调死人。
    weights 是给每一路乘的系数（默认全 1）——数据量变大后关键词那路会多出
    一堆「字面沾边」的候选，把只在向量里靠前的结果挤出去，这时可以给向量
    那路调高一点。**默认不改**，要不要改由 scripts/bench_fusion.py 在评测集上验。
    """
    if weights is not None and len(weights) != len(lists):
        weights = None
    score: dict[int, float] = {}
    for idx, ranked in enumerate(lists):
        w = 1.0 if weights is None else weights[idx]
        for rank, (oid, _raw) in enumerate(ranked, start=1):
            score[oid] = score.get(oid, 0.0) + w / (k + rank)
    return sorted(score.items(), key=lambda kv: kv[1], reverse=True)


# ---------------------------------------------------------------- 单路排名

def _mode_of(conn: sqlite3.Connection, mode: str) -> str:
    """把 auto 落成具体模式；要向量但没向量时老实退回关键词。"""
    mode = (mode or "auto").lower()
    if mode not in MODES:
        mode = "auto"
    if mode == "auto":
        return "hybrid" if vectors.available(conn) else "keyword"
    if mode in ("vector", "hybrid") and not vectors.available(conn):
        return "keyword"
    return mode


def _rank_messages(conn, query: str, mode: str, limit: int) -> list[tuple[int, float]]:
    kw = _kw_messages(conn, query, limit * 3) if mode in ("keyword", "hybrid") else []
    vec = vectors.search(conn, query, "message", limit * 3) if mode in ("vector", "hybrid") else []
    if mode == "hybrid":
        return rrf_fuse([kw, vec])[:limit]
    return (kw if mode == "keyword" else vec)[:limit]


def _rank_memes(conn, query: str, mode: str, limit: int) -> list[tuple[int, float]]:
    kw = _kw_memes(conn, query, limit * 3) if mode in ("keyword", "hybrid") else []
    vec = vectors.search(conn, query, "meme", limit * 3) if mode in ("vector", "hybrid") else []
    if mode == "hybrid":
        return rrf_fuse([kw, vec])[:limit]
    return (kw if mode == "keyword" else vec)[:limit]


# ---------------------------------------------------------------- 取正文

def _rows_by_id(conn, table: str, cols: str, ids: list[int]) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT {cols} FROM {table} WHERE id IN ({marks})", ids
    ).fetchall()
    return {int(r["id"]): r for r in rows}


def _hydrate_messages(conn, hits: list[tuple[int, float]]) -> list[dict[str, Any]]:
    by_id = _rows_by_id(
        conn, "messages", "id, ts, session, sender_name, kind, text", [h[0] for h in hits]
    )
    out: list[dict[str, Any]] = []
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


def _hydrate_memes(conn, hits: list[tuple[int, float]]) -> list[dict[str, Any]]:
    by_id = _rows_by_id(
        conn, "memes", "id, pack, tag, caption, keywords, path, file_name", [h[0] for h in hits]
    )
    out: list[dict[str, Any]] = []
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


# ---------------------------------------------------------------- 对外接口

def search_messages(
    conn: sqlite3.Connection, query: str, limit: int = 10, mode: str = "auto"
) -> list[dict[str, Any]]:
    m = _mode_of(conn, mode)
    return _hydrate_messages(conn, _rank_messages(conn, query, m, limit))


def search_memes(
    conn: sqlite3.Connection, query: str, limit: int = 10, mode: str = "auto"
) -> list[dict[str, Any]]:
    m = _mode_of(conn, mode)
    return _hydrate_memes(conn, _rank_memes(conn, query, m, limit))


def _vec_safe(conn, query: str, kind: str, limit: int):
    """向量这一路挂掉时返回 None，让上层退回关键词。

    为什么不直接抛：向量检索要联网（把问题发给硅基流动换成向量）。
    那边抖一下，整站搜索就 500，等于把「关键词能用」这个保底也一起弄没了。
    返回 None 之后 `search_all` 会把这次标成 degraded，前端能看到「这次是降级的结果」。
    """
    try:
        return vectors.search(conn, query, kind, limit)
    except Exception as e:  # noqa: BLE001  网络层任何异常都不该炸掉整个搜索
        print(f"[warn] 向量检索失败，退回关键词: {type(e).__name__}: {e}", flush=True)
        return None


def search_all(
    conn: sqlite3.Connection,
    query: str,
    kind: str = "all",
    limit: int = 10,
    mode: str = "auto",
) -> dict[str, Any]:
    """所有召回支路放进**一次** RRF，融合完再截断。

    早期版本是"消息融合一次、表情包融合一次，再拿两个结果融合一次"——
    融合两遍会把只在向量里排第一的东西稀释掉（它只有 1/61 分，而两轮之间
    还要和表情包抢名次）。现在四路平铺：关键词-消息、向量-消息、关键词-表情包、
    向量-表情包，一次定名次。
    """
    t0 = time.perf_counter()
    kind = (kind or "all").lower()
    m = _mode_of(conn, mode)
    want_msg = kind in ("all", "message", "messages")
    want_meme = kind in ("all", "meme", "memes")
    deep = max(limit * 4, 20)  # 每路多捞一点，融合后才有得选

    # 查询变体：原句 + 扩展说法。扩展**只给关键词那一路**（见 expand.py 顶部说明）：
    # 扩展的用处就是「把问法翻译成记录里真有的字」，而向量那一路每加一句都要多调一次接口。
    queries: list[str] = [query]
    if cfg.MULTI_QUERY:
        try:
            from . import expand
            if expand.available():
                queries += [a for a in expand.expand(conn, query) if a]
        except Exception as e:  # noqa: BLE001  扩展是加分项，挂了不能拖垮搜索
            print(f"[warn] 查询扩展失败，只用原句: {type(e).__name__}: {e}", flush=True)

    lists: list[list[tuple[int, float]]] = []
    kinds: list[str] = []        # 与 lists 一一对应，用来决定这一路的基础权重
    factors: list[float] = []    # 与 lists 一一对应：原句 1.0、扩展说法 MQ_WEIGHT
    degraded: str | None = None
    for qi, qx in enumerate(queries):
        fac = 1.0 if qi == 0 else cfg.MQ_WEIGHT
        kw_on = m in ("keyword", "hybrid")
        vec_on = m in ("vector", "hybrid") and qi == 0
        if want_msg:
            if kw_on:
                lists.append(_kw_messages(conn, qx, deep))
                kinds.append("kw")
                factors.append(fac)
            if vec_on:
                vec = _vec_safe(conn, qx, "message", deep)
                if vec is None:
                    degraded = "向量检索失败，这一路退回关键词"
                else:
                    lists.append(vec)
                    kinds.append("vec")
                    factors.append(fac)
        if want_meme:
            if kw_on:
                lists.append([(i + MEME_OFF, s) for i, s in _kw_memes(conn, qx, deep)])
                kinds.append("kw")
                factors.append(fac)
            if vec_on:
                vec = _vec_safe(conn, qx, "meme", deep)
                if vec is None:
                    degraded = "向量检索失败，这一路退回关键词"
                else:
                    lists.append([(i + MEME_OFF, s) for i, s in vec])
                    kinds.append("vec")
                    factors.append(fac)

    keep = [i for i, x in enumerate(lists) if x]
    lists = [lists[i] for i in keep]
    kinds = [kinds[i] for i in keep]
    factors = [factors[i] for i in keep]
    if not lists:
        ranked: list[tuple[int, float]] = []
    elif len(lists) == 1:
        ranked = lists[0]
    else:
        weights = [
            (cfg.RRF_VEC_W if kd == "vec" else 1.0) * factors[i]
            for i, kd in enumerate(kinds)
        ]
        ranked = rrf_fuse(lists, k=cfg.RRF_K, weights=weights)
    ranked = ranked[:limit]

    msg_ids = [oid for oid, _ in ranked if oid < MEME_OFF]
    meme_ids = [oid - MEME_OFF for oid, _ in ranked if oid >= MEME_OFF]
    msg_rows = _hydrate_messages(conn, [(i, 0.0) for i in msg_ids]) if msg_ids else []
    meme_rows = _hydrate_memes(conn, [(i, 0.0) for i in meme_ids]) if meme_ids else []
    by_key = {(h["type"], h["id"]): h for h in msg_rows + meme_rows}

    hits = []
    for oid, sc in ranked:
        h = by_key.get(("meme", oid - MEME_OFF)) if oid >= MEME_OFF else by_key.get(("message", oid))
        if not h:
            continue
        h = {**h, "score": round(sc, 6)}
        hits.append(h)

    took = (time.perf_counter() - t0) * 1000
    return {
        "query": query,
        "queries": queries,
        "kind": kind,
        "mode": m,
        "took_ms": round(took, 2),
        "count": len(hits),
        "hits": hits,
        "degraded": degraded,
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
        "vector_ok": vectors.available(conn),
        "tags": tags,
        "senders": senders,
        "ts_from": span["a"] if span else "",
        "ts_to": span["b"] if span else "",
    }
