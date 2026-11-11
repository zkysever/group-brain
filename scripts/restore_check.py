"""备份恢复演练：拿一份备份，真的打开它、真的查一次，确认它是有用的。

为什么要有这个脚本：`backup_db.py` 只证明「文件写出来了、行数对得上」。
文件写出来但打不开（截断、坏页、权限）、或者能打开但索引是空的，
这种事只有真恢复一次才会暴露。备份不验证恢复，等于没有备份。

用法：
    python scripts/restore_check.py                      # 用 data/backups 里最新的一份
    python scripts/restore_check.py data/backups/xxx.db  # 指定某一份
    python scripts/restore_check.py --keep               # 演练后把临时副本留着看

设计上的三条：
1. 永远在临时副本上演练，不动备份原件，更不动线上库；
2. 除了看行数，还要**真跑一次检索**——行数对但 FTS 索引空了，是这套系统最典型的坏法；
3. 退出码非 0 才算失败，方便以后接进 CI 或 cron 报警。
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, search  # noqa: E402

BACKUP_DIR = ROOT / "data" / "backups"
NEED_TABLES = {"messages", "memes", "vectors", "messages_fts", "memes_fts"}


def latest_backup() -> Path | None:
    if not BACKUP_DIR.exists():
        return None
    files = sorted(BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def check(src: Path) -> bool:
    print(f"演练对象：{src}")
    print(f"  大小 {src.stat().st_size / 1024 / 1024:.2f} MB")

    tmpdir = Path(tempfile.mkdtemp(prefix="gb_restore_"))
    copy = tmpdir / "restore_test.db"
    shutil.copy2(src, copy)
    print(f"  已在临时目录复制一份：{copy}")

    ok = True
    conn = sqlite3.connect(copy)
    try:
        conn.row_factory = sqlite3.Row

        # 1. 完整性
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"  integrity_check: {integrity}")
        ok &= integrity == "ok"

        # 2. 表齐不齐
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = NEED_TABLES - tables
        print(f"  表齐全: {'是' if not missing else '缺 ' + ', '.join(sorted(missing))}")
        ok &= not missing

        # 3. 行数
        st = search.stats(conn)
        print(f"  行数：消息 {st['messages']} / 表情包 {st['memes']} / 向量 {st['vectors']}")
        ok &= st["messages"] > 0 and st["memes"] > 0

        # 4. FTS 索引和正文条数是否一致
        n_msg = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        print(f"  关键词索引：正文 {n_msg} 行 / 索引 {n_fts} 行 → {'一致' if n_msg == n_fts else '不一致'}")
        ok &= n_msg == n_fts

        # 5. 最要紧的一步：真查一次
        res = search.search_all(conn, "肥鱼", kind="all", limit=3, mode="keyword")
        hits = res.get("hits") or []
        print(f"  真查一次「肥鱼」：{len(hits)} 条（{res.get('took_ms')} ms）")
        for h in hits[:3]:
            print(f"    - [{h.get('type')}] {str(h.get('text') or '')[:40]}")
        ok &= len(hits) > 0

        # 6. 表情包文件在不在这台机器上（备份只存 DB，图在 meme-packs 目录）
        row = conn.execute("SELECT path FROM memes LIMIT 1").fetchone()
        if row:
            p = config.MEME_PACKS_DIR / row["path"]
            exist = p.exists()
            print(f"  表情包文件抽查：{row['path']}")
            print(f"    在 {config.MEME_PACKS_DIR} 下找 → {'在' if exist else '没找到'}")
            if not exist:
                print("    备份里只有数据库，图另存在 meme-packs 目录。恢复时两样都要拷；")
                print("    若这里是服务器，记得带上 MEME_PACKS 环境变量再跑：MEME_PACKS=/opt/group-brain/meme-packs")
    finally:
        conn.close()

    if ok:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("\n[OK] 这份备份能打开、索引齐、查得出东西——真出事的时候它是能用的")
        return True

    print(f"\n[FAIL] 这份备份有问题，临时副本留在 {tmpdir} 里，别删")
    return False


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        src = Path(args[0])
    else:
        src = latest_backup()
        if src is None:
            print(f"[FAIL] {BACKUP_DIR} 里没有任何备份。先跑一次 python scripts/backup_db.py")
            return 1
    if not src.exists():
        print(f"[FAIL] 找不到 {src}")
        return 1
    return 0 if check(src) else 1


if __name__ == "__main__":
    sys.exit(main())
