"""给消息和表情包建向量，写进 vectors 表。

用法：
  python scripts/build_vectors.py                 # 只补还没有向量的
  python scripts/build_vectors.py --force         # 全部删掉重建
  python scripts/build_vectors.py --kind meme     # 只做一类
  python scripts/build_vectors.py --limit 20      # 先拿 20 条试水，不写库

为什么要单独一个脚本、不在启动时顺手算：
向量是一天一变的东西（模型换了就得整批重算），而启动是一天十次的东西。
把"要花 40 秒、要联网、要花钱"的活儿塞进启动流程，等于每次重启都在赌网络。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, embeddings, vectors
from app.config import EMBEDDING_MODEL

BATCH = 16


def message_texts(conn) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT id, text FROM messages WHERE text IS NOT NULL AND text != '' ORDER BY id"
    ).fetchall()
    return [(int(r["id"]), r["text"]) for r in rows]


def meme_texts(conn) -> list[tuple[int, str]]:
    """表情包把 caption + keywords 拼一起——单看 caption 全是"别催我一脸不耐烦"，
    关键词才是"别催 烦 等一下"这种用户真正会打的词。"""
    rows = conn.execute("SELECT id, caption, keywords, tag FROM memes ORDER BY id").fetchall()
    out = []
    for r in rows:
        text = f"{r['caption'] or ''} {r['keywords'] or ''}".strip()
        if not text:
            text = r["tag"] or ""
        out.append((int(r["id"]), text))
    return out


def build(conn, kind: str, pairs: list[tuple[int, str]], force: bool, limit: int) -> int:
    have = set() if force else vectors.existing_ids(conn, kind)
    todo = [(i, t) for i, t in pairs if i not in have]
    if limit:
        todo = todo[:limit]
    if not todo:
        print(f"  {kind}: 都有向量了，跳过")
        return 0

    print(f"  {kind}: 待处理 {len(todo)} 条，模型 {EMBEDDING_MODEL}")
    done = 0
    t0 = time.perf_counter()
    for start in range(0, len(todo), BATCH):
        chunk = todo[start : start + BATCH]
        arr = None
        for attempt in range(1, 4):
            try:
                arr = embeddings.embed([t for _, t in chunk])
                break
            except Exception as e:  # 网络抖动、限速都走这条路
                print(f"    第 {attempt} 次失败：{type(e).__name__}: {e}")
                time.sleep(2 * attempt)
        if arr is None:
            print("    [X] 这一段彻底失败，先停。已完成的已经写库，重跑会接着做。")
            break

        for (oid, _text), vec in zip(chunk, arr):
            vectors.store(conn, kind, oid, vec)
        conn.commit()
        done += len(chunk)
        print(f"    {done}/{len(todo)}  {time.perf_counter() - t0:.1f}s")

    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="删掉重算")
    ap.add_argument("--kind", default="all", choices=["all", "message", "meme"])
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（试水用）")
    args = ap.parse_args()

    if not embeddings.available():
        print("[X] 没找到 SILICONFLOW_API_KEY。")
        print("    本机：存到 %USERPROFILE%\\.dsh\\secrets\\siliconflow-api-key.txt")
        print("    服务器：写进 /opt/group-brain/.env，然后 systemctl restart group-brain")
        raise SystemExit(1)

    conn = db.connect()
    try:
        if args.force:
            if args.kind == "all":
                conn.execute("DELETE FROM vectors")
            else:
                conn.execute("DELETE FROM vectors WHERE kind = ?", (args.kind,))
            conn.commit()
            vectors.clear_cache()

        total = 0
        if args.kind in ("all", "message"):
            total += build(conn, "message", message_texts(conn), args.force, args.limit)
        if args.kind in ("all", "meme"):
            total += build(conn, "meme", meme_texts(conn), args.force, args.limit)

        db.set_meta(conn, "vec_model", EMBEDDING_MODEL)
        conn.commit()
        n_msg = conn.execute("SELECT COUNT(*) FROM vectors WHERE kind='message'").fetchone()[0]
        n_meme = conn.execute("SELECT COUNT(*) FROM vectors WHERE kind='meme'").fetchone()[0]
        print()
        print(f"完成：新增 {total} 条。库里现在 message {n_msg} 条 / meme {n_meme} 条。")
        print("下一步：python scripts/run_eval.py")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
