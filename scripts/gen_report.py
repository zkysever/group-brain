"""生成 A4 学习教练的周报。

用法：
    python scripts/gen_report.py                # 生成「本周」
    python scripts/gen_report.py --week 2026-W45
    python scripts/gen_report.py --week 2026-W45 --dry   # 只打印事实清单，不调模型、不落库

计划任务（每天 02:00 生成上一周的周报）：
    crontab: 0 2 * * 1  cd /opt/group-brain && .venv/bin/python scripts/gen_report.py --week last
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import coach, db  # noqa: E402


def last_week() -> str:
    return coach.iso_week(date.today() - timedelta(days=7))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", default="", help="ISO 周，如 2026-W45；写 last 表示上一周；留空表示本周")
    ap.add_argument("--dry", action="store_true", help="只打印事实清单，不调模型")
    args = ap.parse_args()

    week = args.week.strip()
    if week == "last":
        week = last_week()
    week = week or coach.iso_week()

    conn = db.connect()
    try:
        if args.dry:
            facts = coach.collect_facts(conn, week)
            print(coach.render_facts(facts))
            return 0

        print(f"[..] 正在生成 {week} 的周报")
        res = coach.generate(conn, week, save=True)
        print("[ok] 已存入 reports 表")
        print("-" * 60)
        print(res["text"])
        print("-" * 60)
        print(json.dumps(res["facts"], ensure_ascii=False)[:600] + " …")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
