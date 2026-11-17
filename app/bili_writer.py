"""给视频写评论草稿：喂简介 + 热评进去，让模型写「像真人、有信息增量」的短评。

为什么不让模型随便写：B 站评论区最招人烦的就是「沙发」「支持UP」这种。
实测看了两个涨粉教程的热评，真正被点赞的评论都在**补充具体内容**——
要么指出视频里没说到的点，要么给一个自己的判断。所以提示词里把这三条写死：
①说视频里具体的哪一段/哪个观点 ②给一个自己的判断或补充 ③不超过 60 字，不用套话。
"""
from __future__ import annotations

import json
import re

from . import bili
from . import config as cfg

SYSTEM = "你是一个真实的中文视频观众，只在被视频内容说服或想补充时才留言。"

PROMPT = """视频标题：{title}
UP 主：{owner}
播放/评论：{view} / {reply}
简介：{desc}

评论区热评（按点赞排序，只作参考，不要抄）：
{comments}

请写 {n} 条**你可能会在这个视频下留的评论**，要求：
1. 每条都指向视频里具体的内容（某个观点、某段素材、某个说法），不要泛泛夸；
2. 至少一半要带一个自己的判断或补充（同意/不同意/补充一个例子/指出一个问题）；
3. 每条 15–60 字，口语，不要 emoji 堆砌，不要「沙发」「支持」「三连」这类套话；
4. 不要出现「作为AI」「我是机器人」；
5. 不要写广告、不要带链接、不要点名攻击他人。

只输出 JSON 数组，形如 [{{"text": "...", "why": "这条为什么值得发"}}]"""


def _llm(prompt: str) -> str:
    import urllib.request

    body = json.dumps({
        "model": cfg.DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(cfg.DEEPSEEK_URL, data=body, headers={
        "Authorization": f"Bearer {cfg.DEEPSEEK_KEY}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode())
    return d["choices"][0]["message"]["content"]


def _parse(raw: str, n: int) -> list[dict]:
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:                              # noqa: BLE001
        return []
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        text = (it.get("text") or "").strip()
        if 8 <= len(text) <= 120:
            out.append({"text": text, "why": (it.get("why") or "").strip()[:120]})
        if len(out) >= n:
            break
    return out


def suggest(conn, bvid: str, n: int = 3) -> dict:
    v = bili.view(bvid, conn)
    if v.get("code") != 0:
        return {"ok": False, "error": f"拿不到视频信息（code {v.get('code')}）"}
    stat = v.get("stat") or {}
    cmts = bili.replies(v["aid"], 8, conn)
    cmt_text = "\n".join(f"- {c['like']}赞 {c['user']}：{c['text']}" for c in cmts) or "（没有热评）"
    prompt = PROMPT.format(title=v.get("title"), owner=v.get("owner"),
                           view=stat.get("view"), reply=stat.get("reply"),
                           desc=(v.get("desc") or "")[:400], comments=cmt_text, n=n)
    try:
        raw = _llm(prompt)
    except Exception as e:                         # noqa: BLE001
        return {"ok": False, "error": f"模型调用失败: {type(e).__name__}: {e}"}
    cands = _parse(raw, n)
    if not cands:
        return {"ok": False, "error": "模型没给出可用草稿", "raw": raw[:300]}
    ids = []
    for c in cands:
        ids.append(bili.add_draft(conn, bvid, c["text"], c["why"], aid=v["aid"],
                                  title=v.get("title") or ""))
    return {"ok": True, "bvid": bvid, "title": v.get("title"), "drafts": ids,
            "items": cands}


def posts_today(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM bili_actions WHERE action='comment' AND ok=1 "
        "AND substr(ts,1,10)=date('now','localtime')").fetchone()
    return int(row["c"] if row else 0)


def can_post(conn, aid: int | None = None) -> tuple[bool, str]:
    from . import config as cfg2
    if posts_today(conn) >= cfg2.BILI_POST_PER_DAY:
        return False, f"今天已经发满 {cfg2.BILI_POST_PER_DAY} 条"
    if aid is not None and cfg2.BILI_ONE_PER_VIDEO:
        row = conn.execute(
            "SELECT COUNT(*) c FROM bili_actions WHERE action='comment' AND ok=1 "
            "AND target=? ", (str(aid),)).fetchone()
        if row and row["c"]:
            return False, "这个视频已经评论过了"
    return True, "ok"
