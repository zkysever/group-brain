"""向量检索（v0.4 才真正用上）。

现在这个文件是"接口先摆着、跑得起来但默认不启用"：
  - 没有 SILICONFLOW_API_KEY 时 available() 直接返回 False，全站走关键词检索；
  - 配了 key 之后，`python scripts/build_vectors.py`（v0.4 再写）会把向量灌进 vectors 表。

为什么不现在做：v0.1 的目标是"能搜到东西"，先把召回跑通，
否则你会花两天调 embedding，却连一条评测数据都没有。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import numpy as np

from .config import EMBEDDING_MODEL, SILICONFLOW_KEY

URL = "https://api.siliconflow.cn/v1/embeddings"


class EmbeddingError(RuntimeError):
    """接口出错。带上服务端原话——不带的话排查时只能对着 400 猜。"""


def available() -> bool:
    return bool(SILICONFLOW_KEY)


def _post(body: bytes, timeout: int = 60, attempts: int = 3) -> dict:
    """打一次 embedding 接口，网络抖动和 429/5xx 重试，4xx 直接把服务端原话抛出来。

    为什么必须重试：跑 50 条评测时，任意一条碰上一次抖动，整场评测就断在半路，
    前面的结果全白跑（2026-11-10 就这么断过一次）。
    为什么 4xx 不重试：参数错了重试一百次还是错，只会多花时间。
    为什么要抛原话：`HTTP Error 400: Bad Request` 这七个字，什么都说明不了。
    """
    last: Exception | None = None
    for i in range(attempts):
        req = urllib.request.Request(
            URL,
            data=body,
            headers={
                "Authorization": f"Bearer {SILICONFLOW_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and i < attempts - 1:
                last = e
                time.sleep(1.5 * (i + 1))
                continue
            raise EmbeddingError(f"embedding 接口 {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
                continue
            raise EmbeddingError(f"embedding 接口连不上: {type(e).__name__}: {e}") from e
    raise EmbeddingError(f"embedding 接口重试 {attempts} 次仍失败: {last}")


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
        data = _post(body)
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
