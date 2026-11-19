"""每日挖词：从最近的群聊里找「这个群自己才懂的说法」，上网查明白，记住。

为什么要有这条定时任务
--------------------
插件上报走的是「我没看懂」那条线：它很准，但有个致命漏点 —— 没人问的话，
它永远不会去查。可群里的黑话是一天天攒出来的，不主动挖，第一次被人问到就答不上。

所以每天补一次：睡前把白天的聊天翻一遍，挑几个「圈外人看不懂」的词去学。
学到的词第二天就能用（`/api/lexicon/context` 会带上）。

用法
----
    python scripts/learn_terms.py              # 正常跑（每天最多 8 个词）
    python scripts/learn_terms.py --limit 3    # 少学几个
    python scripts/learn_terms.py --dry        # 只挑不学：先看它想学什么
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, lexicon  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="从最近群聊里挖词并学会它")
    ap.add_argument("--limit", type=int, default=8, help="这一轮最多学几个（默认 8）")
    ap.add_argument("--days", type=int, default=2, help="回看几天（默认 2）")
    ap.add_argument("--dry", action="store_true", help="只挑不学")
    args = ap.parse_args()

    c = db.connect()
    try:
        lexicon.ensure(c)
        before = lexicon.stats(c)
        if args.dry:
            # 只跑「挑词」这一段，不写库：先看看它的眼光
            picked = lexicon.pick(c, limit=args.limit, days=args.days)
            print(json.dumps({"picked": [p["term"] for p in picked],
                              "why": [{"term": p["term"], "quote": p["quote"]} for p in picked]},
                             ensure_ascii=False, indent=2))
            return 0
        res = lexicon.mine(c, limit=args.limit, days=args.days)
        after = lexicon.stats(c)
        print(f"[挖词] 挑到 {len(res.get('picked') or [])} 个：{'、'.join(res.get('picked') or []) or '-'}")
        print(f"[学会] {len(res.get('learned') or [])} 个：{'、'.join(res.get('learned') or []) or '-'}")
        for s in res.get("skipped") or []:
            print(f"  跳过 {s.get('term')}（{s.get('why')}）")
        if res.get("error"):
            print(f"  !! {res['error']}")
        print(f"[词库] {before['terms']} → {after['terms']} 条（今天新增 {after['new_today']}）")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
