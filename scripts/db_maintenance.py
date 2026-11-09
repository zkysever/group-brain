"""数据库维护：把 WAL 合并回主库、清空空洞、报一下体积。

为什么要做：SQLite 默认 WAL 模式，写入先进 `-wal` 文件，等到一定量才合并。
网站跑久了 `-wal` 会涨到几 MB，`-shm` 也在，看着吓人；`VACUUM` 则能把删除留下的空洞还回去。
这两个操作都**只动文件布局，不动数据**，但 `VACUUM` 会短暂锁库，别在高峰期点。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import DB_PATH


def size_of(p: Path) -> str:
    if not p.exists():
        return "-"
    b = p.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return f"{b:.0f}{unit}" if unit != "GB" else f"{b / 1024 / 1024:.1f}MB"
        b /= 1024
    return f"{b:.0f}"


def main() -> None:
    vacuum = "--vacuum" in sys.argv

    print("[..] 处理前")
    for suf in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suf)
        print(f"     {p.name:<22} {size_of(p)}")

    conn = db.connect()
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        print(f"[ok] WAL 已合并回主库: busy={row[0]} log={row[1]} checkpointed={row[2]}")
        n_msg = conn.execute("SELECT count(*) c FROM messages").fetchone()["c"]
        n_meme = conn.execute("SELECT count(*) c FROM memes").fetchone()["c"]
        n_vec = conn.execute("SELECT count(*) c FROM vectors").fetchone()["c"]
        print(f"[ok] 数据: 消息 {n_msg} / 表情包 {n_meme} / 向量 {n_vec}")
    finally:
        conn.close()

    if vacuum:
        # VACUUM 必须在 autocommit 下跑，所以单独开一条连接
        import sqlite3

        raw = sqlite3.connect(DB_PATH)
        try:
            raw.execute("VACUUM")
            print("[ok] VACUUM 完成（空洞已归还）")
        finally:
            raw.close()
    else:
        print("[..] 跳过 VACUUM（要跑就加 --vacuum）")

    print("[..] 处理后")
    for suf in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suf)
        print(f"     {p.name:<22} {size_of(p)}")


if __name__ == "__main__":
    main()
