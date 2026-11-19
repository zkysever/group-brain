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

from . import config as cfg
from . import embeddings
from . import db

# kind -> (stamp, ids, mat)。stamp 里带「条数 + 最大 id + 模型名 + 维度」：
# 只按条数判断的话，`build_vectors.py --force`（DELETE + INSERT、条数一样）
# 之后进程里仍然是旧矩阵，不重启就一直搜旧数据；换模型同维更糟——不报错，
# 余弦全是噪声。GB_DB 指向临时库时也不会再串用真库的缓存。
_cache: dict[str, tuple[tuple[int, int, str, int], list[int], np.ndarray]] = {}


def available(conn: sqlite3.Connection) -> bool:
    """有 key、库里也有向量，向量那一路才算能用。"""
    if not embeddings.available():
        return False
    row = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()
    return bool(row and int(row[0]) > 0)


def clear_cache() -> None:
    _cache.clear()


def _stamp(conn: sqlite3.Connection, kind: str) -> tuple[int, int, str, int]:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(owner_id), 0), COALESCE(MAX(dim), 0)"
        " FROM vectors WHERE kind = ?",
        (kind,),
    ).fetchone()
    n, top, dim = int(row[0]), int(row[1]), int(row[2])
    try:
        model = str(db.get_meta(conn, "vec_model") or "")
    except Exception:  # noqa: BLE001  老库没有 meta 表就算了，stamp 里少一项而已
        model = ""
    return (n, top, model, dim)


def _load(conn: sqlite3.Connection, kind: str) -> tuple[list[int], np.ndarray]:
    stamp = _stamp(conn, kind)
    n, _top, _model, dim = stamp
    hit = _cache.get(kind)
    if hit and hit[0] == stamp:
        return hit[1], hit[2]

    rows = conn.execute(
        "SELECT owner_id, dim, vec FROM vectors WHERE kind = ? ORDER BY owner_id", (kind,)
    ).fetchall()
    ids = [int(r["owner_id"]) for r in rows]
    if rows:
        dims = {int(r["dim"]) for r in rows}
        if len(dims) > 1:
            raise ValueError(
                f"向量维度不一致 {sorted(dims)}（kind={kind}）：请重跑 scripts/build_vectors.py --force"
            )
        if dim and dim != cfg.EMBEDDING_DIM:
            # 换过模型却没重建：不拦的话 `mat @ qv[0]` 会先抛一个难懂的
            # ValueError: all the input array dimensions ... must match
            raise ValueError(
                f"向量维度 {dim} 与当前模型 {cfg.EMBEDDING_MODEL}({cfg.EMBEDDING_DIM}) 不符："
                f"请重跑 scripts/build_vectors.py --force"
            )
        mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
    else:
        mat = np.zeros((0, 0), dtype=np.float32)
    _cache[kind] = (stamp, ids, mat)
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
