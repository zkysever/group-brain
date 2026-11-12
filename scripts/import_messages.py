"""把 AstrBot 的群消息历史灌进库里。

数据源：data/raw/data_v4.db -> platform_message_history
  content 是 JSON: {"type":"user","message":[{"type":"plain","text":"..."},
                                            {"type":"at","user_id":"...","name":"..."}]}

处理规则：
  - plain 段原样收
  - at 段转成 "@名字"
  - image 段转成 "[图片]"，纯净图（只发了一张图）的消息直接跳过
  - 其余段（face / reply 等）忽略

2026-11-12 改成**增量**：
  以前的版本一上来就 `DELETE FROM messages` 再全量重灌。数据一过期（比如停了几天没导），
  中间那几天就永远补不回来——库里只剩最后一次全量导的那批。用户说「别再删它记忆了」，
  所以现在改成只增不改：
    - 库里没有的 → 插入
    - 库里有的但原文变了（机器人改名、消息重发）→ 更新那一行 + 重建那一行的索引
    - 没变的 → 直接跳过，连分词都不做
  想回到老行为（整表重灌）加 `--full`，只在数据真的错乱时用。
"""
from __future__ import annotations

import argparse
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


def _kind_of(raw: str) -> str:
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return str(obj.get("type") or "") if isinstance(obj, dict) else ""


def import_from(src_path: Path | str, full: bool = False) -> dict:
    """从任意一个 AstrBot 库导入，返回 {raw, added, updated, skipped, total}。

    写成函数而不是只在 main 里跑，是为了让 `scripts/sync_messages.py` 能直接复用，
    不用再走一遍「先拷文件、再调命令行」的弯路。
    """
    src_path = Path(src_path)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT id, created_at, user_id, sender_id, sender_name, content "
        "FROM platform_message_history ORDER BY id"
    ).fetchall()
    src.close()

    conn = db.connect()
    try:
        if full:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM messages_fts")
            conn.commit()

        have = {
            int(r["id"]): (r["text"] or "", r["ts"] or "")
            for r in conn.execute("SELECT id, text, ts FROM messages").fetchall()
        }

        added = updated = skipped = 0
        for r in rows:
            text = render_content(r["content"])
            if not text:
                continue
            mid = int(r["id"])
            ts = str(r["created_at"] or "")
            old = have.get(mid)
            if old is not None and old[0] == text and old[1] == ts:
                skipped += 1
                continue

            conn.execute(
                "INSERT OR REPLACE INTO messages("
                "id, ts, session, sender_id, sender_name, kind, text) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    mid,
                    ts,
                    str(r["user_id"] or ""),
                    str(r["sender_id"] or ""),
                    str(r["sender_name"] or ""),
                    _kind_of(r["content"]),
                    text,
                ),
            )
            # fts5 是普通表（不是 external content），改一行要自己删自己那条
            conn.execute("DELETE FROM messages_fts WHERE msg_id = ?", (mid,))
            conn.execute(
                "INSERT INTO messages_fts(bigram, msg_id) VALUES(?, ?)",
                (to_bigrams(text + " " + (r["sender_name"] or "")), mid),
            )
            if old is None:
                added += 1
            else:
                updated += 1

        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        db.set_meta(conn, "messages_imported", str(total))
        conn.commit()
        return {
            "raw": len(rows),
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "total": int(total),
        }
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="把 AstrBot 群消息增量导入 group-brain")
    ap.add_argument("--src", default=str(ASTRBOT_DB), help="AstrBot 的 data_v4.db 路径")
    ap.add_argument("--full", action="store_true", help="整表重灌（只在数据错乱时用）")
    args = ap.parse_args()

    src_path = Path(args.src)
    if not src_path.exists():
        print(f"[X] 找不到聊天数据库: {src_path}")
        print("    把服务器上的 data_v4.db / -wal / -shm 三件一起拷到 data/raw/ 下")
        raise SystemExit(1)

    res = import_from(src_path, full=args.full)
    print(f"[ok] 读到原始消息 {res['raw']} 条")
    print(
        f"[ok] 新增 {res['added']} 条 / 更新 {res['updated']} 条 / 没变 {res['skipped']} 条"
        f" → 库里现在 {res['total']} 条"
    )
    if res["raw"] - res["skipped"] - res["added"] - res["updated"] > 0:
        print("     （差值是被跳过的纯图片/空消息）")


if __name__ == "__main__":
    main()
