"""把 AstrBot 的群消息历史灌进库里。

数据源：data/raw/data_v4.db -> platform_message_history
  content 是 JSON: {"type":"user","message":[{"type":"plain","text":"..."},
                                            {"type":"at","user_id":"...","name":"..."}]}

处理规则：
  - plain 段原样收
  - at 段转成 "@名字"
  - image 段转成 "[图片]"，纯净图（只发了一张图）的消息直接跳过
  - 其余段（face / reply 等）忽略
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db
from app.config import ASTRBOT_DB
from app.text import clean_text, to_bigrams


def render_content(raw: str) -> str:
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    segs = obj.get("message") or []
    if not isinstance(segs, list):
        return ""

    parts: list[str] = []
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        if t == "plain":
            parts.append(str(seg.get("text") or ""))
        elif t == "at":
            name = seg.get("name") or seg.get("user_id") or ""
            parts.append(f"@{name}")
        elif t == "image":
            parts.append("[图片]")
    text = clean_text(" ".join(parts))
    # 只有一张图、一个字都没有的消息，对检索没价值
    if not text or text == "[图片]":
        return ""
    return text


def main() -> None:
    if not ASTRBOT_DB.exists():
        print(f"[X] 找不到聊天数据库: {ASTRBOT_DB}")
        print("    把服务器上的 data_v4.db / -wal / -shm 三件一起拷到 data/raw/ 下")
        raise SystemExit(1)

    src = sqlite3.connect(ASTRBOT_DB)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT id, created_at, user_id, sender_id, sender_name, content "
        "FROM platform_message_history ORDER BY id"
    ).fetchall()
    src.close()
    print(f"[ok] 读到原始消息 {len(rows)} 条")

    conn = db.connect()
    try:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM messages_fts")

        kept = 0
        for r in rows:
            text = render_content(r["content"])
            if not text:
                continue
            try:
                kind = ""
                obj = json.loads(r["content"])
                kind = str(obj.get("type") or "")
            except (TypeError, ValueError):
                pass
            conn.execute(
                "INSERT OR REPLACE INTO messages("
                "id, ts, session, sender_id, sender_name, kind, text) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    r["id"],
                    str(r["created_at"] or ""),
                    str(r["user_id"] or ""),
                    str(r["sender_id"] or ""),
                    str(r["sender_name"] or ""),
                    kind,
                    text,
                ),
            )
            conn.execute(
                "INSERT INTO messages_fts(bigram, msg_id) VALUES(?, ?)",
                (to_bigrams(text + " " + (r["sender_name"] or "")), r["id"]),
            )
            kept += 1

        conn.commit()
        db.set_meta(conn, "messages_imported", str(kept))
        conn.commit()
        print(f"[ok] 有效消息 {kept} 条已入库（跳过 {len(rows) - kept} 条纯图片/空消息）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
