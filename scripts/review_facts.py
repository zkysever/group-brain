"""事实审阅的自动化（v0.19）：把「机器能确定没编」的那批先点头。

用法：
  python scripts/review_facts.py                # 只看，不动库（默认 dry-run）
  python scripts/review_facts.py --apply        # 把够分的自动 approved（打 auto=1）
  python scripts/review_facts.py --undo-auto    # 一键撤销所有自动点头的
  python scripts/review_facts.py --min-score 8  # 更保守的门槛
  python scripts/review_facts.py --limit 50     # 只看前 50 条待审

为什么默认 dry-run：这一步动的是「什么算群里的常识」，宁可见一眼再执行。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, facts  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写下够分的那批（默认只报告）")
    ap.add_argument("--reject-junk", action="store_true", help="同时把明显没用的自动拒绝")
    ap.add_argument("--undo-auto", action="store_true", help="撤销所有自动点头/自动拒绝")
    ap.add_argument("--min-score", type=int, default=facts.AUTO_MIN_SCORE)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条待审（0=全部）")
    ap.add_argument("--show", type=int, default=10, help="打印几条样例")
    args = ap.parse_args()

    conn = db.connect()
    try:
        before = facts.stats(conn)
        print(f"[前] 待审 {before['pending']} / 生效 {before['approved']} / 已拒 {before['rejected']}")
        if not before["pending"] and not args.undo_auto:
            print("[ok] 没有待审事实，什么也不用做")
            return

        if args.undo_auto:
            n = facts.undo_auto(conn)
            after = facts.stats(conn)
            print(f"[撤销] {n} 条自动处理的退回待审 → 待审 {after['pending']} / 生效 {after['approved']}")
            return

        res = facts.auto_approve(
            conn, min_score=args.min_score, dry=not args.apply, limit=args.limit
        )
        print(
            f"[点头] {res['scanned']} 条待审，够分（≥{args.min_score}）{res['picked']} 条，"
            f"{'已写入' if args.apply else '未写入（dry-run，加 --apply 才写）'}"
        )
        for d in res["detail"][: max(0, args.show)]:
            print(f"  id={d['id']:<5} 分={d['score']:<3} {'；'.join(d['why'])}")

        junk = facts.auto_reject(conn, dry=not (args.apply and args.reject_junk), limit=args.limit)
        print(
            f"[垃圾] 明显没用的 {junk['picked']} 条"
            + (
                "，已自动拒绝"
                if (args.apply and args.reject_junk)
                else "（想一键清掉加 --apply --reject-junk）"
            )
        )
        after = facts.stats(conn)
        print(f"[后] 待审 {after['pending']} / 生效 {after['approved']} / 已拒 {after['rejected']}")
        if not args.apply:
            print("提示：确认没问题就加 --apply 执行；不满意可以 --undo-auto 全退回。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
