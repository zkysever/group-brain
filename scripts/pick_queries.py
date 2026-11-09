"""补评测集用的翻聊天记录小工具。

写评测集难的不是格式，是"想不起来群里说过啥"。这个脚本把 483 条消息
按 id / 发送人 / 时间 / 正文摊开，你翻着看，看到有意思的就记下来。

用法（在 group-brain 目录下）：

    .venv\\Scripts\\python.exe scripts/pick_queries.py             # 全部，一行一条
    .venv\\Scripts\\python.exe scripts/pick_queries.py --grep 鱼    # 只看含「鱼」的
    .venv\\Scripts\\python.exe scripts/pick_queries.py --rand 25    # 随机 25 条，换换口味
    .venv\\Scripts\\python.exe scripts/pick_queries.py --id 8      # 看第 8 条的完整正文和前后 3 条
    .venv\\Scripts\\python.exe scripts/pick_queries.py --no-bot     # 只看真人说的话

挑到想要的了，就照这个格式往 eval/queries.jsonl 里加一行：

    {"q": "你自己会怎么问", "expect": ["结果里一定能出现的一小段原文"]}
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DB_PATH


def load(no_bot: bool) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sql = "SELECT id, ts, sender_name, kind, text FROM messages"
    if no_bot:
        sql += " WHERE kind != 'bot'"
    sql += " ORDER BY ts"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


def show(rows: list[sqlite3.Row], width: int = 60) -> None:
    for r in rows:
        text = " ".join((r["text"] or "").split())
        if len(text) > width:
            text = text[:width] + "…"
        ts = (r["ts"] or "?")[5:16]
        print(f'{r["id"]:>4}  {r["sender_name"] or "-":<10}{ts}  {text}')
    print(f"\n共 {len(rows)} 条")


def main() -> None:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--grep", default="", help="只显示正文里含这个词的")
    ap.add_argument("--rand", type=int, default=0, help="随机抽 N 条")
    ap.add_argument("--id", type=int, default=0, help="看这一条和它的前后各 3 条")
    ap.add_argument("--no-bot", action="store_true", help="过滤掉机器人自己的话")
    ap.add_argument("--width", type=int, default=60, help="每条正文截断到多少字")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[X] 找不到数据库: {DB_PATH}")
        print("    先跑一次 双击启动.bat，或者 python scripts/init_db.py")
        raise SystemExit(1)

    rows = load(args.no_bot)

    if args.id:
        idx = next((i for i, r in enumerate(rows) if r["id"] == args.id), None)
        if idx is None:
            print(f"[X] 没有 id={args.id} 这条")
            raise SystemExit(1)
        for r in rows[max(0, idx - 3) : idx + 4]:
            mark = ">>" if r["id"] == args.id else "  "
            print(f'{mark}{r["id"]:>4}  {r["sender_name"] or "-":<10}{(r["ts"] or "?")[5:16]}')
            print(f'    {r["text"]}')
            print()
        return

    if args.grep:
        key = args.grep.lower()
        rows = [r for r in rows if key in (r["text"] or "").lower()]

    if args.rand:
        rows = random.sample(rows, min(args.rand, len(rows)))
        rows.sort(key=lambda r: r["ts"] or "")

    show(rows, args.width)


if __name__ == "__main__":
    main()
