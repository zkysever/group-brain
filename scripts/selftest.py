"""CI 里跑的自检：不联网、不需要真数据，只验证「代码还能跑通」这件事。

为什么要有它：这个项目的真正测试是 eval/ 里那两套评测，但它们需要真实群聊
数据库（4.8MB，不进 git）和 API key，CI 上跑不了。于是退一步，用临时库把
「建表 → 写入 → 关键词检索 → 取上下文 → 统计」这条主链路走一遍。

它拦住的是这类事故：
  - 改了 SQL / 表结构，忘了迁移 → 建表或查询报错
  - 改了 search / db 的函数签名，调用方没跟着改 → ImportError / TypeError
  - 分词函数写崩了 → 检索返回空

用法：
    python scripts/selftest.py
    GB_DB=/tmp/ci.db python scripts/selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前设置：config 在导入时就把 DB_PATH 定下来了。
if not os.environ.get("GB_DB"):
    os.environ["GB_DB"] = str(Path(tempfile.gettempdir()) / "gb_selftest.db")

from app import db, search  # noqa: E402
from app.config import DB_PATH  # noqa: E402


FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [ok] " if ok else "  [X]  ") + name + (f"    {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    db.init()
    conn = db.connect()
    try:
        # 1. 建表
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
        need = {"messages", "memes", "vectors", "meta", "reports", "messages_fts", "memes_fts"}
        check("建表", need <= tables, "缺: " + ", ".join(sorted(need - tables)) if need - tables else "")

        # 2. 写入两条消息 + 一张表情包
        rows = [
            ("2026-01-01 10:00:00.000000", "147", "user", "@大肥鱼 我想吃鱼片", None),
            ("2026-01-01 10:00:30.000000", "bot", "bot", "鱼片的花样可太多了——酸菜鱼、水煮鱼", None),
        ]
        cur = conn.executemany(
            "INSERT INTO messages (ts, sender_name, kind, text, session) VALUES (?,?,?,?,?)", rows
        )
        conn.execute(
            "INSERT INTO memes (tag, path, caption, keywords) VALUES (?,?,?,?)",
            ("angry", "pack/memes/angry/1.png", "鼓起脸生气，冒气", "生气 愤怒 哼"),
        )
        conn.commit()
        check("写入消息", cur.rowcount == 2, f"{cur.rowcount} 条")

        # 3. FTS 索引：用和导入脚本同一套分词写法灌一遍
        from app.text import to_bigrams

        for r in conn.execute("SELECT id, sender_name, text FROM messages").fetchall():
            conn.execute(
                "INSERT INTO messages_fts (bigram, msg_id) VALUES (?, ?)",
                (to_bigrams(r["text"] + " " + (r["sender_name"] or "")), r["id"]),
            )
        for r in conn.execute("SELECT id, caption, keywords FROM memes").fetchall():
            conn.execute(
                "INSERT INTO memes_fts (bigram, meme_id) VALUES (?, ?)",
                (to_bigrams((r["caption"] or "") + " " + (r["keywords"] or "")), r["id"]),
            )
        conn.commit()

        # 4. 关键词检索（最高频的用法）
        hit = search.search_messages(conn, "鱼片", limit=5)
        check("搜消息「鱼片」", len(hit) >= 1, f"{len(hit)} 条")
        hit2 = search.search_memes(conn, "生气", limit=5)
        check("搜表情包「生气」", len(hit2) >= 1, f"{len(hit2)} 条")

        # 5. 混合检索在没向量时必须老实退回关键词（不能报错、不能返回空）
        mixed = search.search_all(conn, "鱼片", mode="auto", limit=5)
        check("auto 模式无向量时退回关键词", len(mixed) >= 1, f"{len(mixed)} 条")

        # 6. 取上下文
        mid = conn.execute("SELECT id FROM messages ORDER BY id LIMIT 1").fetchone()["id"]
        win = db.window(conn, mid, before=1, after=1)
        check("取上下文 window()", len(win) == 2, f"{len(win)} 条")

        # 7. 统计接口用到的 stats()
        st = search.stats(conn)
        check("stats()", st.get("messages") == 2 and st.get("memes") == 1, str(st))
    finally:
        conn.close()

    print()
    if FAILS:
        print(f"[X] {len(FAILS)} 项没过：" + "、".join(FAILS))
        raise SystemExit(1)
    print("[OK] 自检全过（用到的是临时库，没碰你的数据）")


if __name__ == "__main__":
    main()
