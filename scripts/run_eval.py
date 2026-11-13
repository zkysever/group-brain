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
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfg, db, search

from app.config import EVAL_DIR

TOP_K = 5
MODES = ("keyword", "vector", "hybrid")
RERANK = False
DEPTH = 20
_RERANK_URL = "https://api.siliconflow.cn/v1/rerank"
_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


def _hit_text(h: dict) -> str:
    return ((h.get("text") or "") + " " + (h.get("keywords") or "")).strip()


def rerank_hits(question: str, hits: list[dict]) -> list[dict]:
    """用硅基流动的 reranker 重排。失败就原样返回——评测不能因为一次网络抖动整轮作废。"""
    if len(hits) < 2:
        return hits
    key = cfg.SILICONFLOW_KEY
    if not key:
        print("[warn] 没配 SILICONFLOW_API_KEY，跳过重排")
        return hits
    body = json.dumps({
        "model": _RERANK_MODEL, "query": question,
        "documents": [_hit_text(h) for h in hits],
        "top_n": len(hits), "return_documents": False,
    }).encode()
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                _RERANK_URL, data=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            order = [d["index"] for d in data["results"]]
            return [hits[i] for i in order if 0 <= i < len(hits)]
        except Exception as e:  # noqa: BLE001
            if attempt:
                print(f"[warn] 重排失败，保持原序: {e}")
                return hits
            time.sleep(1.5)
    return hits



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
    depth = max(TOP_K, DEPTH) if RERANK else TOP_K
    for c in cases:
        res = search.search_all(conn, c["q"], "all", depth, mode)
        got = res["hits"]
        if RERANK:
            got = rerank_hits(c["q"], got)
        rank = hit_rank(got, c.get("expect") or [])
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
    ap.add_argument("--mq", default="", choices=["", "on", "off"], help="多查询扩展：on/off 覆盖配置（默认关），留空=按 config")
    ap.add_argument("--rerank", action="store_true", help="先取 --depth 条候选，再用 bge-reranker-v2-m3 重排后取前 --top 名")
    ap.add_argument("--depth", type=int, default=20, help="重排的候选池大小，默认 20")
    args = ap.parse_args()

    if args.rerank:
        global RERANK, DEPTH
        RERANK = True
        DEPTH = max(args.depth, args.top * 2)
        print(f"[重排] 开（池 {DEPTH} 条，{_RERANK_MODEL}）")

    if args.mq:
        from app import config as _cfg
        _cfg.MULTI_QUERY = args.mq == "on"
        print(f"[多查询扩展] {'开' if _cfg.MULTI_QUERY else '关'}（MQ_MAX={_cfg.MQ_MAX} MQ_WEIGHT={_cfg.MQ_WEIGHT}）")

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
