"""只重建关键词索引（FTS5），不碰原始数据。

什么时候用：
  - 改了分词规则（`app/text.py` 的 `to_bigrams`）之后——索引和查询必须同一套切法，
    只改一边等于全部搜不到；
  - FTS 表和正表对不上号了（不信就看看两边 count 差多少）。

为什么单独写一个脚本：`import_messages.py` 要读 AstrBot 的原始库，
而这个不需要——服务器上根本没有原始库（数据是从本机灌进去的），
所以重建索引这件事必须能在「只有结果表」的机器上单独跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.text import to_bigrams


def main() -> None:
    conn = db.connect()
    try:
        before_msg = conn.execute("SELECT count(*) c FROM messages_fts").fetchone()["c"]
        before_meme = conn.execute("SELECT count(*) c FROM memes_fts").fetchone()["c"]
        print(f"[..] 重建前 FTS: 消息 {before_msg} 条 / 表情包 {before_meme} 条")

        conn.execute("DELETE FROM messages_fts")
        conn.execute("DELETE FROM memes_fts")

        n_msg = 0
        for r in conn.execute("SELECT id, text, sender_name FROM messages").fetchall():
            conn.execute(
                "INSERT INTO messages_fts(bigram, msg_id) VALUES(?, ?)",
                (to_bigrams((r["text"] or "") + " " + (r["sender_name"] or "")), r["id"]),
            )
            n_msg += 1

        n_meme = 0
        for r in conn.execute("SELECT id, caption, keywords, tag FROM memes").fetchall():
            text = f"{r['caption'] or ''} {r['keywords'] or ''}".strip() or (r["tag"] or "")
            conn.execute(
                "INSERT INTO memes_fts(bigram, meme_id) VALUES(?, ?)",
                (to_bigrams(text), r["id"]),
            )
            n_meme += 1

        conn.commit()
        db.set_meta(conn, "fts_rebuilt", str(n_msg + n_meme))
        conn.commit()
        print(f"[ok] 重建完成: 消息 {n_msg} 条 / 表情包 {n_meme} 条")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
