"""从群聊里抽「事实」草稿（v0.14）。

它不直接生效：写进 facts 表时 status=pending，要人去 /facts 点头。
这一步的成本约等于把 564 条消息读一遍（30 次左右的接口调用，几毛钱），
所以默认按段跑、可以 --since 只抽新消息。

设计上刻意「抽得少」：
- 只抽**稳定的、关于某人某物的**结论（谁爱喝什么、群名改成了什么、谁是谁的谁）；
- 不抽情绪、不抽一次性的请求（「给我一张图」不是事实）、不抽时间敏感的话；
- 每条必须带 quote（原话）和 msg_ids —— 没有依据的「事实」就是模型的想象，
  留着它等于给系统装了一个稳定的谣言源。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db, facts  # noqa: E402

BATCH = 20          # 一次喂多少条消息
MAX_BATCHES = 60    # 一次运行最多跑多少批（防止手滑把额度跑光）
QUOTE_CHARS = 200

PROMPT = """下面是一个 QQ 群的真实聊天片段（压成「id | 发送者: 内容」一行一条）。

请抽出其中**关于某个人或某个东西的稳定信息**——偏好、身份、习惯、群里的设定和规矩、
反复被提起的结论。宁可多抽一点也不要漏，但每条都必须能用原话支撑。

可以抽：
- 某人的偏好/习惯（「147 爱喝扎啤」）
- 某人的身份、关系、外号（「大肥鱼是群里被调侃的对象」）
- 群里的设定和规矩（「群名改成了满级路人」）
- 某件反复被提起的事的结论（「群主说不再加新功能」）

不要抽：
- 纯情绪和语气词（「哈哈」「坏了」「？」）
- 一次性的请求（「给我一张图」——这是请求不是事实）
- 时间敏感的话（「今天好热」）
- 你自己推断出来的、原话里没有的东西

每条必须给：subject（谁/什么，2–10 字）、claim（一句话结论，不超过 30 字）、
quote（支撑它的原话片段，20 字内）、msg_ids（来源消息 id 数组）。

只输出**严格的 JSON 数组**，不要解释、不要 markdown 代码块：
[{"subject": "147", "claim": "爱喝扎啤", "quote": "扎啤", "msg_ids": [565]}]

一条都没有就输出：[]"""


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
        raise RuntimeError("没配 DEEPSEEK_API_KEY，抽不了")
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


def _parse(raw: str) -> list[dict]:
    """模型的输出不能全信：先抓第一个 [ ... ]，再逐条校验字段。"""
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if not isinstance(it, dict):
            continue
        subject = str(it.get("subject") or "").strip()
        claim = str(it.get("claim") or "").strip()
        if not subject or not claim:
            continue
        ids = it.get("msg_ids")
        if isinstance(ids, list):
            ids = [x for x in ids if isinstance(x, int)]
        else:
            ids = []
        out.append(
            {
                "subject": subject,
                "claim": claim,
                "quote": str(it.get("quote") or "").strip()[:QUOTE_CHARS],
                "msg_ids": ids,
            }
        )
    return out


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="从群聊抽事实草稿（写进 facts 表，pending 待审）")
    ap.add_argument("--limit", type=int, default=BATCH * MAX_BATCHES, help="最多读多少条消息")
    ap.add_argument("--since", default="", help="只抽这个时间之后的消息，如 2026-11-01")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--dry", action="store_true", help="只打印，不写库")
    ap.add_argument("--new", action="store_true", help="只抽上次以后的新消息（cron 用这个）")
    args = ap.parse_args()

    conn = db.connect()
    last_id = int(db.get_meta(conn, "facts_last_id", "0") or 0)
    sql = "SELECT id, ts, sender_name, text FROM messages WHERE text <> ''"
    params: list = []
    if args.new:
        sql += " AND id > ?"
        params.append(last_id)
        print(f"[增量] 上次抽到消息 id={last_id}")
    if args.since:
        sql += " AND ts >= ?"
        params.append(args.since)
    sql += " ORDER BY id LIMIT ?" if args.new else " ORDER BY ts LIMIT ?"
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("[!] 没有新消息可抽")
        return
    max_id = max(r["id"] for r in rows)

    total = {"added": 0, "dup": 0, "bad": 0}
    t0 = time.time()
    batches = 0
    for i in range(0, len(rows), args.batch):
        chunk = rows[i : i + args.batch]
        batches += 1
        block = _msg_block(chunk)
        if not block.strip():
            continue
        try:
            raw = _call(PROMPT + "\n\n" + block)
        except Exception as e:  # 单批失败不该让整轮白跑
            print(f"[warn] 第 {batches} 批失败：{type(e).__name__}: {e}")
            continue
        items = _parse(raw)
        if args.dry:
            print(f"--- 第 {batches} 批（{chunk[0]['id']}..{chunk[-1]['id']}）抽出 {len(items)} 条")
            for it in items:
                print(f"    {it['subject']}｜{it['claim']}  ← {it['quote']}")
            continue
        r = facts.add_candidates(conn, items)
        for k in total:
            total[k] += r[k]
        print(
            f"[ok] 第 {batches} 批 {chunk[0]['id']}..{chunk[-1]['id']}："
            f"抽出 {len(items)} 条，新增 {r['added']}（重复 {r['dup']}，无效 {r['bad']}）"
        )

    if not args.dry:
        db.set_meta(conn, "facts_last_id", str(max_id))
        st = facts.stats(conn)
        print(
            f"\n[完成] 批数 {batches}、耗时 {time.time()-t0:.0f}s；"
            f"本轮新增 {total['added']}、重复 {total['dup']}、无效 {total['bad']}"
        )
        print(f"[现状] 待审 {st['pending']} / 生效 {st['approved']} / 已拒 {st['rejected']}")
        print(f"[游标] facts_last_id = {max_id}（下次 --new 从这里往后抽）")
        print("下一步：打开 /facts 一条条点头")
    conn.close()


if __name__ == "__main__":
    main()
