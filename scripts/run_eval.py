"""评测：跑 eval/queries.jsonl，报 Recall@5 / MRR。

判定方式故意做得"笨"：expect 里任何一个子串出现在前 5 条结果里就算命中。
好处是不用人工给每条消息打 id 标签；坏处是对"换个说法"不敏感——
等评测集长到 50 条时再换成按 message id 精确判定（v0.3 的事）。

用法：python scripts/run_eval.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, search
from app.config import EVAL_DIR

TOP_K = 5


def hit_rank(hits: list[dict], expect: list[str]) -> int:
    """返回命中的名次（1 起），没命中返回 0。"""
    for i, h in enumerate(hits[:TOP_K], start=1):
        text = (h.get("text") or "") + " " + (h.get("keywords") or "")
        if any(e and e in text for e in expect):
            return i
    return 0


def main() -> None:
    path = EVAL_DIR / "queries.jsonl"
    if not path.exists():
        print(f"[X] 找不到评测集: {path}")
        raise SystemExit(1)

    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        cases.append(json.loads(line))

    conn = db.connect()
    hits = mrr = 0.0
    rows = []
    try:
        for c in cases:
            res = search.search_all(conn, c["q"], "all", TOP_K)
            rank = hit_rank(res["hits"], c.get("expect") or [])
            if rank:
                hits += 1
                mrr += 1.0 / rank
            rows.append((c["q"], rank, res["took_ms"]))
    finally:
        conn.close()

    n = max(len(cases), 1)
    print(f"{'查询':<16}{'名次':<8}{'耗时ms':<10}")
    print("-" * 36)
    for q, rank, ms in rows:
        mark = str(rank) if rank else "miss"
        print(f"{q:<16}{mark:<8}{ms:<10}")
    print("-" * 36)
    print(f"样本数      {len(cases)}")
    print(f"Recall@{TOP_K}    {hits / n:.3f}  ({int(hits)}/{len(cases)})")
    print(f"MRR         {mrr / n:.3f}")
    print()
    print("基线就是基线，别急着美化。v0.4 上向量 + RRF 之后，同一份评测集")
    print("Recall@5 至少要涨 15%，涨不动说明向量那条路对你这批数据没用。")


if __name__ == "__main__":
    main()
