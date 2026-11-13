"""评测：跑 eval/queries.jsonl，报 Recall@5 / MRR。

判定方式故意做得"笨"：expect 里任何一个子串出现在前 5 条结果里就算命中。
好处是不用人工给每条消息打 id 标签；坏处是对"换个说法"不敏感——
所以它衡量的是"用户能不能一眼看到想要的"，不是纯粹的语义相似度。

用法：
  python scripts/run_eval.py                  # 三种模式并排对比（有向量时）
  python scripts/run_eval.py --mode keyword   # 只看关键词
  python scripts/run_eval.py --mode hybrid    # 只看融合
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, search

from app.config import EVAL_DIR

TOP_K = 5
MODES = ("keyword", "vector", "hybrid")


def hit_rank(hits: list[dict], expect: list[str]) -> int:
    """返回命中的名次（1 起），没命中返回 0。"""
    for i, h in enumerate(hits[:TOP_K], start=1):
        text = (h.get("text") or "") + " " + (h.get("keywords") or "")
        if any(e and e in text for e in expect):
            return i
    return 0


def run(conn, cases: list[dict], mode: str) -> tuple[list[tuple], float, float]:
    hits = mrr = 0.0
    rows = []
    for c in cases:
        res = search.search_all(conn, c["q"], "all", TOP_K, mode)
        rank = hit_rank(res["hits"], c.get("expect") or [])
        if rank:
            hits += 1
            mrr += 1.0 / rank
        rows.append((c["q"], rank, res["took_ms"], res["mode"]))
    n = max(len(cases), 1)
    return rows, hits / n, mrr / n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="", choices=["", *MODES], help="留空=三种并排")
    ap.add_argument("--queries", default="", help="评测集文件（默认 eval/queries.jsonl；难集用 eval/queries_hard.jsonl）")
    ap.add_argument("--top", type=int, default=5, help="取前几名算命中，默认 5（线上 /api/search 默认 limit=10，口径要对齐就传 10）")
    args = ap.parse_args()

    global TOP_K
    if args.top >= 1:
        TOP_K = args.top

    path = Path(args.queries) if args.queries else EVAL_DIR / "queries.jsonl"
    if not path.is_absolute():
        cand = EVAL_DIR / path.name
        path = cand if cand.exists() else path
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
    try:
        modes = [args.mode] if args.mode else list(MODES)
        results = {m: run(conn, cases, m) for m in modes}
        n = max(len(cases), 1)

        if len(modes) == 1:
            m = modes[0]
            rows, recall, mrr = results[m]
            print(f"模式 {m}")
            print(f"{'查询':<24}{'名次':<8}{'耗时ms':<10}")
            print("-" * 46)
            for q, rank, ms, _ in rows:
                print(f"{q:<24}{str(rank) if rank else 'miss':<8}{ms:<10}")
            print("-" * 46)
            print(f"样本数      {len(cases)}")
            print(f"Recall@{TOP_K}    {recall:.3f}  ({int(recall * n)}/{len(cases)})")
            print(f"MRR         {mrr:.3f}")
            return

        # ---- 三种并排
        head = f"{'查询':<24}"
        for m in modes:
            head += f"{m:<10}"
        print(head)
        print("-" * (24 + 10 * len(modes)))
        for i, c in enumerate(cases):
            line = f"{c['q']:<24}"
            for m in modes:
                rank = results[m][0][i][1]
                line += f"{str(rank) if rank else 'miss':<10}"
            print(line)
        print("-" * (24 + 10 * len(modes)))

        for m in modes:
            rows, recall, mrr = results[m]
            avg_ms = sum(r[2] for r in rows) / n
            print(f"{m:<10} Recall@{TOP_K} {recall:.3f}  MRR {mrr:.3f}  平均 {avg_ms:.0f}ms")

        base = results["keyword"][1]
        hyb = results["hybrid"][1]
        gain = (hyb - base) / base * 100 if base else 0.0
        print()
        print(f"融合 vs 关键词：Recall@{TOP_K} {base:.3f} → {hyb:.3f}（{gain:+.1f}%，目标 ≥ +15%）")

        # 哪些救回来了、哪些反而掉了——这比总数字有用得多
        fixed, broke = [], []
        for i, c in enumerate(cases):
            k = results["keyword"][0][i][1]
            h = results["hybrid"][0][i][1]
            if k == 0 and h:
                fixed.append(f"  救回  {c['q']}（融合后第 {h} 名）")
            elif k and h == 0:
                broke.append(f"  掉队  {c['q']}（关键词第 {k} 名 → 融合后没进前 5）")
        if fixed:
            print("融合救回的：")
            print("\n".join(fixed))
        if broke:
            print("融合搞坏的：")
            print("\n".join(broke))
        if not fixed and not broke:
            print("两种模式的名次集合完全一样。")
            print("这时候要么向量没用上（看 /api/stats 的 vectors 数），要么数据太少看不出差别。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
