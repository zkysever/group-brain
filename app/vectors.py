"""向量检索：bge-m3 向量入库 + 暴力余弦。

605 条数据（483 消息 + 122 表情包）全塞内存也就 2.5MB，一次点乘几毫秒。
这个规模上向量数据库（Qdrant/Milvus/pgvector）纯属给自己找活儿干：
多一个服务、多一个端口、多一份要备份的东西，换来的加速在这个数据量下看不见。
等真到几万条再来谈 ANN。

注意：余弦只比"意思像不像"，不比"字面对不对"。所以它单独用一定比关键词差，
必须两路一起 RRF 融合——单一向量检索最大的坑就在这儿。
"""
from __future__ import annotations

import sqlite3

import numpy as np

from . import embeddings

# kind -> (count, ids, mat)，count 变了就说明库更新过，缓存作废
_cache: dict[str, tuple[int, list[int], np.ndarray]] = {}


def available(conn: sqlite3.Connection) -> bool:
    """有 key、库里也有向量，向量那一路才算能用。"""
    if not embeddings.available():
        return False
    row = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()
    return bool(row and int(row[0]) > 0)


def clear_cache() -> None:
    _cache.clear()


def _load(conn: sqlite3.Connection, kind: str) -> tuple[list[int], np.ndarray]:
    n = int(
        conn.execute("SELECT COUNT(*) FROM vectors WHERE kind = ?", (kind,)).fetchone()[0]
    )
    hit = _cache.get(kind)
    if hit and hit[0] == n:
        return hit[1], hit[2]

    rows = conn.execute(
        "SELECT owner_id, vec FROM vectors WHERE kind = ? ORDER BY owner_id", (kind,)
    ).fetchall()
    ids = [int(r["owner_id"]) for r in rows]
    if rows:
        mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
    else:
        mat = np.zeros((0, 0), dtype=np.float32)
    _cache[kind] = (n, ids, mat)
    return ids, mat


def search(
    conn: sqlite3.Connection, query: str, kind: str = "message", limit: int = 20
) -> list[tuple[int, float]]:
    """向量召回，返回 [(owner_id, 余弦)]，余弦越接近 1 越像。"""
    query = (query or "").strip()
    if not query or not embeddings.available():
        return []

    qv = embeddings.embed([query])
    if qv is None:
        return []

    ids, mat = _load(conn, kind)
    if not ids or mat.size == 0:
        return []

    sims = mat @ qv[0]
    k = min(limit, len(ids))
    idx = np.argpartition(-sims, k - 1)[:k]
    idx = idx[np.argsort(-sims[idx])]
    return [(ids[int(i)], float(sims[int(i)])) for i in idx]


def store(
    conn: sqlite3.Connection, kind: str, owner_id: int, vec: np.ndarray
) -> None:
    v = np.asarray(vec, dtype=np.float32)
    conn.execute(
        "INSERT INTO vectors(kind, owner_id, dim, vec) VALUES(?,?,?,?) "
        "ON CONFLICT(kind, owner_id) DO UPDATE SET dim=excluded.dim, vec=excluded.vec",
        (kind, int(owner_id), int(v.shape[0]), v.tobytes()),
    )


def existing_ids(conn: sqlite3.Connection, kind: str) -> set[int]:
    return {
        int(r[0])
        for r in conn.execute(
            "SELECT owner_id FROM vectors WHERE kind = ?", (kind,)
        ).fetchall()
    }
