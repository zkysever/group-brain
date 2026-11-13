"""给每条群聊消息补一句「它在说什么」（v0.17）。

为什么做这个：难集剩下的 8 条 miss 里，有一半是「问法和答法表达距离太远」——
用户问「鱼什么时候才会开口说话」，群里那句话是「如果现在不@他，他是不会说话的」；
两次说的是同一件事，但字面几乎没有重叠，关键词和向量都够不着。

这层不动检索参数，而是**给索引侧补一种说法**：每条消息先用模型写一句
「这句话在说什么」，把这句话也建成 FTS 索引，查询时多一路检索。

刻意不喂评测集：那个「它在说什么」只能从消息本身推，不能看问题长什么样——
否则就是拿答案当特征，数字会好看但线上没用。
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402

BATCH = 20
MAX_BATCHES = 80
GLOSS_CHARS = 40


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

PROMPT = """下面是 QQ 群里的真实消息（一行一条：id | 发送者: 内容）。

请给**每一条**写一句「它在说什么」，要求：
1. 不超过 25 字，用群里说话的白话，不要书面语；
2. 把这句话**可能被怎么问起**也写进去：如果消息是「不@他，他是不会说话的」，
   就写成「鱼要被人@才会说话，不@就不说」；
3. 含表情包/图片的消息，写它想表达的情绪或用途（「发了个生气甩手的表情」）；
4. 不要评论、不要加「该用户表示」这种前缀、不要写发送者名字。

只输出**严格的 JSON 数组**，形如：
[{"id": 8, "gloss": "想吃鱼片，点名要大肥鱼给"}]

不要解释、不要 markdown 代码块。"""

# 第二种写法：不写「它在说什么」，而写「别人会怎么问起它」。
# 思路是把桥修在**问句侧**：索引里存的是问法的样子，用户真的那样问时字面对得上。
PROMPT_ASK = """下面是 QQ 群里的真实消息（一行一条：id | 发送者: 内容）。

请给**每一条**写 2 个「别人可能会怎么问起这条消息」的问句，要求：
1. 每条不超过 15 字，用群友会打的字，不要书面语；
2. 问句里**不要出现答案的原词**，要像真的在找东西时那样问
   （比如消息是「如果现在不@他，他是不会说话的」→「鱼什么时候才开口说话？」）；
3. 不要写发送者名字，不要回答问题。

两个问句用「；」连成一个字符串。

只输出**严格的 JSON 数组**，形如：
[{"id": 317, "gloss": "鱼什么时候才开口说话？；不@鱼它就不理人吗？"}]

不要解释、不要 markdown 代码块。"""


def _msg_block(rows) -> str:
    lines = []
    for r in rows:
        text = (r["text"] or "").replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f'{r["id"]} | {r["sender_name"] or "?"}: {text[:120]}')
    return "\n".join(lines)


def _call(prompt: str) -> str:
    import requests

    if not config.DEEPSEEK_KEY:
        raise RuntimeError("没配 DEEPSEEK_API_KEY，写不了句意")
    r = requests.post(
        config.DEEPSEEK_URL,
        json={
            "model": config.DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": "你只输出严格 JSON，不要任何解释文字。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        headers={"Authorization": f"Bearer {config.DEEPSEEK_KEY}"},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _parse(raw: str, ids: set[int]) -> list[tuple[int, str]]:
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[tuple[int, str]] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        try:
            mid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        gloss = str(it.get("gloss") or "").strip().replace("\n", " ")
        if mid not in ids or not gloss:
            continue
        out.append((mid, gloss[:GLOSS_CHARS]))
    return out


def _ensure_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS message_glosses (
            msg_id INTEGER PRIMARY KEY,
            gloss TEXT NOT NULL,
            ts TEXT NOT NULL,
            model TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS glosses_fts USING fts5(bigram, msg_id UNINDEXED, tokenize = "unicode61");
        """
    )
    conn.commit()


def rebuild_fts(conn) -> int:
    """把 gloss 的二元组灌进 glosses_fts（整体重建，和 messages_fts 一个套路）。"""
    from app.text import to_bigrams

    conn.execute("DELETE FROM glosses_fts")
    rows = conn.execute("SELECT msg_id, gloss FROM message_glosses").fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO glosses_fts(bigram, msg_id) VALUES (?, ?)",
            (to_bigrams(r["gloss"]), r["msg_id"]),
        )
    conn.commit()
    return len(rows)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="给每条消息写一句「它在说什么」，建 gloss 索引")
    ap.add_argument("--limit", type=int, default=BATCH * MAX_BATCHES)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--missing", action="store_true", help="只补还没有 gloss 的消息（默认就是只补缺的）")
    ap.add_argument("--all", action="store_true", help="忽略已有 gloss，全部重写")
    ap.add_argument("--dry", action="store_true", help="只打印，不写库")
    ap.add_argument(
        "--style",
        default="say",
        choices=["say", "ask"],
        help="say=写「它在说什么」（默认）；ask=写「别人会怎么问起它」，把桥修在问句侧",
    )
    args = ap.parse_args()
    prompt = PROMPT_ASK if args.style == "ask" else PROMPT

    conn = db.connect()
    _ensure_schema(conn)

    have = {r[0] for r in conn.execute("SELECT msg_id FROM message_glosses").fetchall()}
    sql = "SELECT id, ts, sender_name, text FROM messages WHERE text <> '' AND text <> '[Image]'"
    if not args.all:
        sql += " ORDER BY id"
    rows = [r for r in conn.execute(sql).fetchall() if args.all or r["id"] not in have]
    rows = rows[: args.limit]
    if not rows:
        print("[!] 没有需要补的消息")
        return

    t0 = time.time()
    written = 0
    for i in range(0, len(rows), args.batch):
        chunk = rows[i : i + args.batch]
        ids = {r["id"] for r in chunk}
        block = _msg_block(chunk)
        try:
            raw = _call(prompt + "\n\n" + block)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 第 {i // args.batch + 1} 批失败：{type(e).__name__}: {e}")
            continue
        pairs = _parse(raw, ids)
        if args.dry:
            print(f"--- {chunk[0]['id']}..{chunk[-1]['id']} 得到 {len(pairs)}/{len(chunk)} 条")
            for mid, gloss in pairs[:3]:
                print(f"    {mid}: {gloss}")
            continue
        for mid, gloss in pairs:
            conn.execute(
                "INSERT OR REPLACE INTO message_glosses(msg_id, gloss, ts, model) VALUES (?,?,?,?)",
                (mid, gloss, _now(), config.DEEPSEEK_MODEL),
            )
        conn.commit()
        written += len(pairs)
        print(
            f"[ok] {chunk[0]['id']}..{chunk[-1]['id']}：{len(pairs)}/{len(chunk)} 条 "
            f"（累计 {written}，{time.time() - t0:.0f}s）"
        )

    if args.dry:
        return
    n = rebuild_fts(conn)
    st = conn.execute("SELECT COUNT(*) FROM message_glosses").fetchone()[0]
    print(f"\n[完成] 写入 {written} 条，gloss 表共 {st} 条，索引 {n} 条，耗时 {time.time() - t0:.0f}s")
    conn.close()


if __name__ == "__main__":
    main()
