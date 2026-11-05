"""建表。可以重复跑，不会删数据。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import DB_PATH


def main() -> None:
    db.init()
    print(f"[ok] 数据库已就绪: {DB_PATH}")
    conn = db.connect()
    try:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "ORDER BY name"
            ).fetchall()
        ]
        print("[ok] 表:", ", ".join(tables))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
