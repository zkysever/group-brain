"""扫 RRF 的两个旋钮：名次折扣 K 和向量那一路的权重 W。

背景：数据从 483 涨到 564 条之后，48 条评测集里「让鱼帮我把人开盒」从第 4 名
掉出了前 5——向量那路把它排第 1，但关键词那路多出一堆「字面沾边」的候选，
RRF 一平均就把它挤掉了。问题是：该不该给向量加权？

做法：把每套评测集在整张网格上都跑一遍，**两套一起看**。
只看 48 条那套会调出一个「专门修这一条」的参数，那是过拟合。

    python scripts/bench_fusion.py
    python scripts/bench_fusion.py --grid k=20,60 w=1,1.5,2

注意：查询向量只算一次（同一句话在网格里会重复出现），后面都在本地点乘，
所以 62 条查询只会调 62 次 embedding 接口。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config as cfg
from app import db, embeddings, search, vectors  # noqa: E402
from app.config import EVAL_DIR  # noqa: E402

SETS = [
    ("48 条（原集，开卷）", EVAL_DIR / "queries.jsonl"),
    ("14 条（难集，换说法）", EVAL_DIR / "queries_hard.jsonl"),
]


def load_cases(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def hit_text(h: dict) -> str:
    """和 run_eval.py 的 hit_rank 完全一致：表情包的正文在 text 里（就是 caption），关键词另算。"""
    return (h.get("text") or "") + " " + (h.get("keywords") or "")


def score_run(cases: list[dict], limit: int = 5) -> tuple[float, float, list[str]]:
    conn = db.connect()
    try:
        hits = 0
        mrr = 0.0
        misses = []
        for case in cases:
            r = search.search_all(conn, case["q"], kind="all", limit=limit, mode="hybrid")
            rank = None
            for i, h in enumerate(r["hits"], start=1):
                text = hit_text(h)
                if any(e in text for e in case["expect"]):
                    rank = i
                    break
            if rank:
                hits += 1
                mrr += 1.0 / rank
            else:
                misses.append(case["q"])
        n = max(len(cases), 1)
        return hits / n, mrr / n, misses
    finally:
        conn.close()


def cached_embeddings(queries: list[str]) -> None:
    """把每句查询的向量算一次，之后 vectors.search 直接命中缓存。"""
    cache: dict[str, np.ndarray] = {}
    real_embed = embeddings.embed

    def fake_embed(texts, batch=16):  # noqa: ANN001
        todo = [t for t in texts if t not in cache]
        if todo:
            got = real_embed(todo, batch=batch)
            if got is None:
                return None
            for t, v in zip(todo, got):
                cache[t] = np.asarray(v, dtype=np.float32)
        return np.vstack([cache[t] for t in texts])

    embeddings.embed = fake_embed  # type: ignore[assignment]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", default="k=10,20,60,100 w=1,1.5,2,3")
    args = ap.parse_args()

    ks, ws = [60], [1.0]
    for part in args.grid.split():
        if part.startswith("k="):
            ks = [int(x) for x in part[2:].split(",")]
        elif part.startswith("w="):
            ws = [float(x) for x in part[2:].split(",")]

    sets = [(name, load_cases(p)) for name, p in SETS if p.exists()]
    all_queries = sorted({c["q"] for _, cases in sets for c in cases})
    print(f"评测集 {len(sets)} 套 / 查询 {len(all_queries)} 条；先把查询向量算好（只算一次）")
    cached_embeddings(all_queries)
    for q in all_queries:
        vectors.search  # noqa: B018  (确认模块已就位，不实际调用)
    # 预热：真的把每条查询过一遍，让缓存填满
    conn = db.connect()
    try:
        for q in all_queries:
            search.search_all(conn, q, kind="all", limit=5, mode="hybrid")
    finally:
        conn.close()

    header = "  K   W  " + "  ".join(f"{name[:14]:>16}" for name, _ in sets)
    print(header)
    rows = []
    for k, w in itertools.product(ks, ws):
        cfg.RRF_K = k
        cfg.RRF_VEC_W = w
        cells = []
        for _name, cases in sets:
            recall, mrr, _miss = score_run(cases)
            cells.append(f"R@5 {recall:.3f} M {mrr:.3f}")
        rows.append((k, w, cells))
        print(f"{k:4d} {w:4.1f}  " + "  ".join(f"{c:>16}" for c in cells))

    # 找「两套都不比现状差、且总和最高」的那组
    base = next((r for r in rows if r[0] == 60 and r[1] == 1.0), None)
    print("\n基线（K=60 W=1.0）：", base[2] if base else "（不在网格里）")
    print("提示：选参数的标准不是「48 条那套最高」，而是**两套一起看**；")
    print("      只提升一套、另一套掉下来的，基本都是过拟合。")


if __name__ == "__main__":
    main()
