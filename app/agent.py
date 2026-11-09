"""A2：让模型自己决定要不要检索。

前面 v0.5 是「用户打字 → 关键词+向量召回 → 排序」的固定流水线；
这里换一种控制方式：把检索包成**工具**，交给 DeepSeek 自己判断什么时候调、调什么。

三个工具是刻意选的，覆盖三种典型情况：
  search_memory  用户问「谁说过…」「上次那个…」      → 需要翻群聊
  search_meme    用户说「来个表情」「有生气的图吗」  → 需要翻图库
  get_time       用户问「今天几号」                  → 根本不需要检索

真正要观察的不是回答好不好看，而是**它会犯错**：
  过度调用（明明只要说句话，非要去翻一遍库）、
  不调用（该翻的时候凭印象编）、
  指代解消失败（「那个」到底指哪句）。
这三个就是 A2 的全部难点，评测集也是围着它们设计的。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

import requests

from . import config as cfg
from . import search

MAX_ROUNDS = 3          # 超过就停手：再转下去只是在烧 token
HIT_CHARS = 120         # 单条结果截断长度，防止工具输出把上下文顶爆
MAX_HITS = 5

SYSTEM = """你是「群聊记忆体」的助手，只能通过工具查下面这份数据：
一个 QQ 群的真实聊天记录，和一套表情包图库。

规则（按重要性）：
1. 不知道就查，不要凭印象编。编出来的话比查不到更糟。
2. 引用群聊内容时尽量贴原话，别改写成书面语。
3. 找不到就直接说找不到，不要硬凑。
4. 回答用中文，简短，三句以内为佳。
5. 只是打个招呼、问个好，不需要调用任何工具。"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "在 QQ 群聊历史里检索消息。当用户问「谁说过…」「上次那个…」"
                "「有没有人提过…」，或任何需要看群里原话的问题时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词，尽量用群里可能出现的说法"},
                    "limit": {"type": "integer", "description": "返回几条，默认 5，最大 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_meme",
            "description": (
                "在表情包图库里按描述检索图片，返回图片链接。"
                "当用户想要一张表情、问「有没有…的图」，或需要用图回话时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "描述想要什么情绪或画面的图"},
                    "limit": {"type": "integer", "description": "返回几张，默认 3，最大 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "取当前时间（服务器所在时区，UTC+8）。用户问今天几号、现在几点、星期几时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def available() -> bool:
    return bool(cfg.DEEPSEEK_KEY)


def _chat(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """打一次 DeepSeek。带 tools 就是「你先想想要不要调工具」。

    会重试：跑评测时遇到过 `SSLError: Max retries exceeded`——国内到 api.deepseek.com
    偶尔就这么一下。一次抖动不该让整条对话报错，所以 3 次、指数退避。
    注意只重试网络层和 5xx/429，别把 400（参数错）也重试，那样只会更快地把额度烧完。
    """
    payload = {
        "model": cfg.DEEPSEEK_MODEL,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {cfg.DEEPSEEK_KEY}"}

    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.post(cfg.DEEPSEEK_URL, json=payload, headers=headers, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"上游 {r.status_code}: {r.text[:120]}")
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
            if attempt == 2:
                break
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def _trim_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """工具输出专门收窄：模型只需要「够判断」的信息，不需要全量。"""
    out = []
    for h in hits:
        text = (h.get("text") or "").strip()
        if len(text) > HIT_CHARS:
            text = text[:HIT_CHARS] + "…"
        item = {
            "type": h.get("type"),
            "text": text,
            "who": h.get("sender_name") or "",
            "ts": (h.get("ts") or "")[:16],
        }
        if h.get("type") == "meme":
            item["tag"] = h.get("tag") or ""
            item["url"] = h.get("url") or ""
        out.append(item)
    return out


def _run_tool(name: str, args: dict[str, Any], conn) -> tuple[str, dict[str, Any]]:
    """执行一个工具，返回 (喂回模型的字符串, 给前端看的抽屉条目)。"""
    t0 = time.perf_counter()
    if name == "search_memory":
        q = str(args.get("query") or "").strip()
        limit = min(int(args.get("limit") or MAX_HITS), 10)
        res = search.search_all(conn, q, "message", limit, "auto")
        hits = _trim_hits(res["hits"])
        payload = {"query": q, "count": len(hits), "hits": hits}
        note = {"tool": name, "args": {"query": q, "limit": limit}, "n": len(hits), "mode": res["mode"]}
    elif name == "search_meme":
        q = str(args.get("query") or "").strip()
        limit = min(int(args.get("limit") or 3), 5)
        res = search.search_all(conn, q, "meme", limit, "auto")
        hits = _trim_hits(res["hits"])
        payload = {"query": q, "count": len(hits), "hits": hits}
        note = {"tool": name, "args": {"query": q, "limit": limit}, "n": len(hits), "mode": res["mode"]}
    elif name == "get_time":
        now = datetime.now().astimezone()
        payload = {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "weekday": "一二三四五六日"[now.weekday()], "tz": str(now.tzinfo)}
        note = {"tool": name, "args": {}, "n": 1, "mode": "-"}
    else:
        payload = {"error": f"没有这个工具: {name}"}
        note = {"tool": name, "args": args, "n": 0, "mode": "-"}

    note["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return json.dumps(payload, ensure_ascii=False), note


def run(conn, question: str, max_rounds: int = MAX_ROUNDS) -> dict[str, Any]:
    """跑一轮「问 → 模型决定调工具 → 拿结果再回答」，最多 max_rounds 轮工具调用。"""
    if not available():
        return {"ok": False, "error": "没配 DEEPSEEK_API_KEY，A2 用不了", "answer": "", "trace": []}

    t0 = time.perf_counter()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    trace: list[dict[str, Any]] = []
    answer = ""

    for rnd in range(max_rounds + 1):
        try:
            data = _chat(messages)
        except requests.RequestException as e:
            return {
                "ok": False,
                "error": f"模型接口出错: {type(e).__name__}: {e}",
                "answer": "",
                "trace": trace,
                "rounds": rnd,
                "took_ms": round((time.perf_counter() - t0) * 1000, 1),
            }

        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []

        if not calls:
            answer = (msg.get("content") or "").strip()
            messages.append({"role": "assistant", "content": answer})
            break

        if rnd == max_rounds:
            # 转够轮数还在要工具：停手，把话说清楚，别假装完成
            answer = "（工具调用超过 %d 轮，我先停下来。可能问题需要拆得更具体一点。）" % max_rounds
            trace.append({"tool": "STOP", "args": {}, "n": 0, "ms": 0, "mode": "-",
                          "error": f"超过 {max_rounds} 轮仍在请求工具"})
            break

        # 把 assistant 这条（带 tool_calls）原样放回对话，DeepSeek 要求一一对应
        messages.append(
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
        )
        for c in calls:
            fn = c.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result, note = _run_tool(name, args, conn)
            trace.append(note)
            messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": result})

    took = round((time.perf_counter() - t0) * 1000, 1)
    names = [t["tool"] for t in trace]
    return {
        "ok": True,
        "question": question,
        "answer": answer,
        "trace": trace,
        "tool_calls": names,
        "rounds": len([n for n in names if n != "STOP"]),
        "took_ms": took,
        "model": cfg.DEEPSEEK_MODEL,
    }
