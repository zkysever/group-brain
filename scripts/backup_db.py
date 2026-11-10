"""把数据库备份一份，并删掉太旧的备份。服务器上每天定时跑。

为什么不用 `cp group_brain.db`：
    库开着 WAL，直接 cp 只拷到主文件，最近的写入还在 -wal 里，拷出来是旧的、
    甚至是不一致的。sqlite3 自带 `Connection.backup()`，它会在一个读事务里
    把一致性快照写到目标文件，正好干这件事。

用法：
    python scripts/backup_db.py                 # 默认留 14 份
    python scripts/backup_db.py --keep 30       # 留 30 份
    python scripts/backup_db.py --dir /opt/gb-backups
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DB_PATH  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DB_PATH.parent / "backups"))
    ap.add_argument("--keep", type=int, default=14, help="保留最近几份（默认 14 = 两周）")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[X] 没有数据库文件: {DB_PATH}")
        raise SystemExit(1)

    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"group_brain-{stamp}.db"

    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)          # 一致性快照，WAL 里的内容也会带上
        dst.commit()
    finally:
        dst.close()
        src.close()

    # 校验：备份文件能打开，且三张主表的行数对得上
    chk = sqlite3.connect(target)
    try:
        counts = {
            t: chk.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("messages", "memes", "vectors")
        }
    finally:
        chk.close()

    size_mb = target.stat().st_size / 1024 / 1024
    print(f"[ok] 备份: {target.name}  ({size_mb:.2f} MB)  " +
          f"消息 {counts['messages']} / 表情包 {counts['memes']} / 向量 {counts['vectors']}")

    olds = sorted(out_dir.glob("group_brain-*.db"))
    for f in olds[: max(0, len(olds) - args.keep)]:
        f.unlink()
        print(f"[ok] 删旧备份: {f.name}")
    print(f"[ok] 现在共 {len(list(out_dir.glob('group_brain-*.db')))} 份，保留上限 {args.keep}")


if __name__ == "__main__":
    main()
