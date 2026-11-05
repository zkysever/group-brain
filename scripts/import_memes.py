"""把本机两套表情包 pack 的 caption 灌进库里。

数据源：%USERPROFILE%\\.dsh\\meme-packs\\<pack>\\index.db
  memes(path, tag, file_name, caption, keywords, mtime, captioned_at)
图片本身不搬，库里存绝对路径，网页通过 /api/meme/{id} 现读现发。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import MEME_PACKS_DIR
from app.text import clean_text, to_bigrams


def iter_packs():
    if not MEME_PACKS_DIR.exists():
        print(f"[warn] 找不到表情包目录: {MEME_PACKS_DIR}")
        return
    for pack in sorted(MEME_PACKS_DIR.iterdir()):
        idx = pack / "index.db"
        if idx.is_file():
            yield pack, idx


def main() -> None:
    conn = db.connect()
    try:
        packs = list(iter_packs())
        if not packs:
            print("[warn] 一套 pack 都没找到，跳过")
            return

        conn.execute("DELETE FROM memes")
        conn.execute("DELETE FROM memes_fts")

        total = 0
        for pack, idx in packs:
            src = sqlite3.connect(idx)
            src.row_factory = sqlite3.Row
            rows = src.execute(
                "SELECT path, tag, file_name, caption, keywords FROM memes"
            ).fetchall()
            src.close()

            n, missing = 0, 0
            for r in rows:
                rel = (r["path"] or "").replace("\\", "/")
                abs_path = (pack / rel).resolve()
                if not abs_path.exists():
                    missing += 1
                    continue
                caption = clean_text(r["caption"] or "")
                keywords = clean_text(r["keywords"] or "")
                tag = (r["tag"] or "").strip()
                cur = conn.execute(
                    "INSERT OR REPLACE INTO memes(pack, tag, caption, keywords, path, file_name) "
                    "VALUES(?,?,?,?,?,?)",
                    (pack.name, tag, caption, keywords, str(abs_path), r["file_name"] or abs_path.name),
                )
                mid = cur.lastrowid
                bigram = to_bigrams(f"{caption} {keywords} {tag}")
                conn.execute(
                    "INSERT INTO memes_fts(bigram, meme_id) VALUES(?, ?)", (bigram, mid)
                )
                n += 1
            total += n
            print(f"[ok] {pack.name}: 导入 {n} 张" + (f"，{missing} 张文件缺失已跳过" if missing else ""))

        conn.commit()
        db.set_meta(conn, "memes_imported", str(total))
        conn.commit()
        print(f"[ok] 表情包合计 {total} 条已入库")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
