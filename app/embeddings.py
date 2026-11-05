"""向量检索（v0.4 才真正用上）。

现在这个文件是"接口先摆着、跑得起来但默认不启用"：
  - 没有 SILICONFLOW_API_KEY 时 available() 直接返回 False，全站走关键词检索；
  - 配了 key 之后，`python scripts/build_vectors.py`（v0.4 再写）会把向量灌进 vectors 表。

为什么不现在做：v0.1 的目标是"能搜到东西"，先把召回跑通，
否则你会花两天调 embedding，却连一条评测数据都没有。
"""
from __future__ import annotations

import json
import urllib.request

import numpy as np

from .config import EMBEDDING_MODEL, SILICONFLOW_KEY


def available() -> bool:
    return bool(SILICONFLOW_KEY)


def embed(texts: list[str], batch: int = 16) -> np.ndarray | None:
    """调硅基流动 bge-m3，返回 (n, dim) float32。没 key 返回 None。"""
    if not available() or not texts:
        return None

    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        body = json.dumps(
            {"model": EMBEDDING_MODEL, "input": chunk, "encoding_format": "float"}
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://api.siliconflow.cn/v1/embeddings",
            data=body,
            headers={
                "Authorization": f"Bearer {SILICONFLOW_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vectors.extend([d["embedding"] for d in data["data"]])

    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def cosine_topk(query_vec: np.ndarray, mat: np.ndarray, k: int = 20) -> list[tuple[int, float]]:
    """暴力余弦。几十万条以内 numpy 点乘够快，不用上向量数据库。"""
    if mat.size == 0:
        return []
    sims = mat @ query_vec
    idx = np.argsort(-sims)[:k]
    return [(int(i), float(sims[i])) for i in idx]
