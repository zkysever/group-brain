"""A2：让模型自己决定要不要检索。

前面 v0.5 是「用户打字 → 关键词+向量召回 → 排序」的固定流水线；
这里换一种控制方式：把检索包成**工具**，交给 DeepSeek 自己判断什么时候调、调什么。

工具是刻意选的，覆盖几种典型情况：
  search_memory  用户问「谁说过…」「上次那个…」      → 需要翻群聊
  search_facts   用户问「某人喜欢什么」「群里规矩」  → 需要查人工确认过的事实
  search_meme    用户说「来个表情」「有生气的图吗」  → 需要翻图库
  get_time       用户问「今天几号」                  → 根本不需要检索
  web_search     用户问群外的事（版本号、新闻、文档）→ 需要上网（v0.18 加）

真正要观察的不是回答好不好看，而是**它会犯错**：
  过度调用（明明只要说句话，非要去翻一遍库）、
  不调用（该翻的时候凭印象编）、
  指代解消失败（「那个」到底指哪句）。
这三个就是 A2 的全部难点，评测集也是围着它们设计的。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from . import config as cfg
from . import facts
from . import learn
from . import lexicon
from . import router
from . import search
from . import websearch

# 规则前置开关：AGENT_PREFETCH=0 可以关掉，用来做 A/B 对比。
# 关掉之后行为和 v0.9 完全一致——这种开关要留着，不然改了东西没法证明改好了。
PREFETCH = os.environ.get("AGENT_PREFETCH", "1") != "0"

MAX_ROUNDS = cfg.AGENT_MAX_ROUNDS   # 超过就停手：再转下去只是在烧 token
HIT_CHARS = cfg.AGENT_HIT_CHARS     # 单条结果截断长度，防止工具输出把上下文顶爆
MAX_HITS = cfg.AGENT_MAX_HITS
HISTORY_TURNS = cfg.AGENT_HISTORY_TURNS   # 往后带几轮对话（默认 20，以前是 3）
HISTORY_CHARS = cfg.AGENT_HISTORY_CHARS   # 单条历史截断

SYSTEM = """你是「群聊记忆体」的助手，只能通过工具查下面这份数据：
一个 QQ 群的真实聊天记录，和一套表情包图库。

规则（按重要性）：
1. 不知道就查，不要凭印象编。编出来的话比查不到更糟。
2. 引用群聊内容时尽量贴原话，别改写成书面语。
3. 找不到就直接说找不到，不要硬凑。
4. 回答用中文，简短，三句以内为佳。
5. 只是打个招呼、问个好，不需要调用任何工具。
6. 如果上面有「刚才的对话」，那么用户说的「刚才那句」「上一条」「他」指的就是它——
   能从上文直接回答的就直接回答，不必再查；上文里真的找不到对应物时，
   直接反问「你说的是哪句」，**不要瞎猜一个去搜**。
7. 问的是群外的事（软件版本、新闻、官方文档、百科概念）时用 web_search 上网查；
   问的是群里的事（谁说过什么、某人喜好、群里的梗）**不要**上网，那些在群聊记录里。
   用 web_search 回答时**必须带上来源链接**；搜索结果和群聊记录冲突时，以群聊记录为准。
8. 用户自己贴了链接、又问的是**页面上才有的事实**（标题、数字、播放量、时间、版本号）时，
   即使你记得上次抓过，也要先用 fetch_url 抓一次再答；抓完确实没有这些数字，就说没有。"""

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
            "name": "search_facts",
            "description": (
                "查「群里确认过的常识」：某人的偏好/身份/外号、群里的规矩和设定。只在这两种时候调它："
                "①问某人是什么样的人、喜欢什么；②问群里的规矩/设定/约定。"
                "**它的库很小（只有人工确认过的几十条），找不到原话**——"
                "问「谁说过某句话」「聊过什么」时不要调它，直接调 search_memory。"
                "调一次没结果就换 search_memory，别重复调它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词，比如「147 喝什么」「群名」"},
                    "limit": {"type": "integer", "description": "返回几条，默认 5，最大 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "上网搜公开资料，返回标题、链接和摘要。**只用于群外的知识**："
                "软件/库的版本号、发布时间、官方文档、新闻、百科概念、报错信息。"
                "问群里谁说过什么、某人的喜好、群里的梗时**不要**用它——那些用 search_memory / search_facts。"
                "它的摘要很短，只够判断「是不是这个」；需要细节时在回答里给出链接让用户自己点。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，用关键词，别写整句寒暄"},
                    "limit": {"type": "integer", "description": "返回几条，默认 4，最大 8"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "抓一个网页的正文（前 1200 字）。两种时候用：①web_search 给了明确该点的链接、"
                "而摘要不够回答；②**用户自己贴了链接**，问的是页面上才有的事实（标题、数字、时间）。"
                "**一次对话最多抓 2 个页面**。不要用它抓群聊记录（那些用 search_memory），也不要拿它当爬虫。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓的链接，必须是 http/https；用户贴的和 web_search 给的都行"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_lexicon",
            "description": (
                "查群里的新词、梗、缩写、外号是什么意思。这个库是它自己从群里学的："
                "碰到没见过的词就调它；库里没有时它会**顺手上网查一次并记住**。"
                "问「XX 是什么意思」「XX 是谁的外号」先调它，再考虑 web_search。"
                "它只解释词，不解释某句话——问原话还是用 search_memory。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "要查的词本身，别带问句"},
                    "context": {"type": "string", "description": "可选：这个词出现的那句话，帮助判断它在这个群里的意思"},
                },
                "required": ["term"],
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


def _now_cn() -> datetime:
    """北京时间。服务器 TZ 常常是 UTC，`astimezone()` 会给错日期/星期。

    优先用 zoneinfo（Linux 自带 tzdata）；Windows 上没装 tzdata 包会抛
    ZoneInfoNotFoundError，退回固定 +8（中国不用夏令时，固定偏移没有坑）。
    """
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone(timedelta(hours=8)))


def _tools_for_this_run() -> list[dict[str, Any]]:
    """按开关裁工具表。

    A6 接进来之后服务器评测从 0.960/0 掉到 0.900/0.167——多一个工具，
    模型就多一次「我该不该查」的犹豫，也更容易绕圈（三条失败都是调了 3 次）。
    所以拆成两个旋钮：LEXICON_HINT（白送上下文，默认开）和
    LEXICON_TOOL（主动查词，默认关）。关掉工具等于回到原来的六个。
    """
    if cfg.LEXICON_TOOL:
        return TOOLS
    return [t for t in TOOLS if (t.get("function") or {}).get("name") != "lookup_lexicon"]


def _chat(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """打一次 DeepSeek。带 tools 就是「你先想想要不要调工具」。

    会重试：跑评测时遇到过 `SSLError: Max retries exceeded`——国内到 api.deepseek.com
    偶尔就这么一下。一次抖动不该让整条对话报错，所以 3 次、指数退避。
    注意只重试网络层和 5xx/429；400（参数错、消息顺序坏了）重试三次只是
    「多烧 4.5 秒 + 多占一个工作线程」，然后把同一个错误再抛一遍。
    """
    payload = {
        "model": cfg.DEEPSEEK_MODEL,
        "messages": messages,
        "tools": _tools_for_this_run(),
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
        except requests.HTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            # 只重试「上游可恢复」的；4xx 参数类错误直接抛，别把额度烧完
            if code is not None and code not in (429, 500, 502, 503, 504):
                raise
            last = e
            if attempt == 2:
                break
            time.sleep(1.5 * (attempt + 1))
        except (requests.ConnectionError, requests.Timeout) as e:
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


def _lim(v: Any, default: int, hi: int) -> int:
    """把模型给的 limit 洗成一个安全的整数：非法回落、下界 1、上界 hi。

    以前是 `min(int(args.get("limit") or MAX_HITS), 10)`，只有上界：
    `{"limit": "五张"}` 直接 ValueError → 整个请求 500；
    `{"limit": -1}` 会一路传进 SQLite，而**负 LIMIT 等于不限制**，
    再加上 _trim_hits 只截单条长度不截条数，一次就能把上下文顶爆。
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hi))


def _run_tool_raw(name: str, args: dict[str, Any], conn) -> tuple[str, dict[str, Any]]:
    """执行一个工具，返回 (喂回模型的字符串, 给前端看的抽屉条目)。"""
    t0 = time.perf_counter()
    if name == "search_memory":
        q = str(args.get("query") or "").strip()
        limit = _lim(args.get("limit") or MAX_HITS, MAX_HITS, 10)
        res = search.search_all(conn, q, "message", limit, "auto")
        hits = _trim_hits(res["hits"])
        payload = {"query": q, "count": len(hits), "hits": hits}
        note = {"tool": name, "args": {"query": q, "limit": limit}, "n": len(hits), "mode": res["mode"]}
    elif name == "search_meme":
        q = str(args.get("query") or "").strip()
        limit = _lim(args.get("limit") or 3, 3, 5)
        res = search.search_all(conn, q, "meme", limit, "auto")
        hits = _trim_hits(res["hits"])
        payload = {"query": q, "count": len(hits), "hits": hits}
        note = {"tool": name, "args": {"query": q, "limit": limit}, "n": len(hits), "mode": res["mode"]}
    elif name == "get_time":
        # 服务器 TZ 常常是 UTC：`datetime.now().astimezone()` 在北京时间 00:00–08:00
        # 会给出前一天的日期和星期，而工具描述承诺的是 UTC+8。直接钉死时区。
        now = _now_cn()
        payload = {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "weekday": "一二三四五六日"[now.weekday()], "tz": "UTC+8"}
        note = {"tool": name, "args": {}, "n": 1, "mode": "-"}
    elif name == "web_search":
        q = str(args.get("query") or "").strip()
        limit = _lim(args.get("limit") or 4, 4, 8)
        rows = websearch.search(conn, q, limit)
        hits = [
            {
                "type": "web",
                "title": r["title"],
                "url": r["url"],
                "text": r["snippet"],
                "who": r["source"],
                "ts": "",
            }
            for r in rows
        ]
        payload = {"query": q, "count": len(hits), "hits": hits}
        if not hits:
            # 和 facts 的 0 结果同一个道理：明确告诉模型「这条路断了」，
            # 否则它会换个词反复搜，一次对话能把 token 烧掉一半。
            payload["note"] = "联网没搜到结果。不要再调 web_search，直接说「网上没查到」，或在群里找原话。"
        note = {"tool": name, "args": {"query": q, "limit": limit}, "n": len(hits), "mode": "web"}
    elif name == "fetch_url":
        u = str(args.get("url") or "").strip()
        try:
            page = websearch.fetch(u)
            payload = {
                "url": page["url"],
                "title": page["title"],
                "text": page["text"],
            }
            if page.get("via") == "browser":
                payload["via"] = "真浏览器渲染"
            n = 1
        except Exception as exc:  # noqa: BLE001 —— 抓不到就当没这回事
            payload = {"url": u, "error": f"抓不到: {type(exc).__name__}: {exc}",
                       "note": "这个链接抓不下来。换一个结果，或者直接告诉用户看链接。"}
            n = 0
        note = {"tool": name, "args": {"url": u[:80]}, "n": n, "mode": "web",
                "via": page.get("via") if n else None}
    elif name == "lookup_lexicon":
        term = str(args.get("term") or "").strip()[:20]
        row = lexicon.get(conn, term)
        if row:
            payload = {
                "term": row["term"],
                "gloss": row["gloss"],
                "kind": row["kind"],
                "confidence": round(float(row["confidence"]), 2),
                "source": row["source"],
            }
            n = 1
        else:
            # 库里没有：现场学一次（上网搜 + 写解释）。学不会就直说，
            # 让模型自己决定是再 web_search 还是承认不知道。
            try:
                learned = lexicon.lookup(conn, term, str(args.get("context") or "")[:60])
            except Exception:  # noqa: BLE001
                learned = None
            if learned:
                payload = {
                    "term": learned["term"],
                    "gloss": learned["gloss"],
                    "kind": learned["kind"],
                    "confidence": round(float(learned["confidence"]), 2),
                    "source": "web(刚学的)",
                }
                n = 1
            else:
                payload = {
                    "term": term,
                    "note": "词库里没有，上网也没弄明白。要么用 web_search 再试，要么直接说不知道。",
                }
                n = 0
        note = {"tool": name, "args": {"term": term}, "n": n, "mode": "lexicon"}
    elif name == "search_facts":
        q = str(args.get("query") or "").strip()
        limit = _lim(args.get("limit") or MAX_HITS, MAX_HITS, 10)
        rows = facts.search(conn, q, limit)
        # 事实条目本来就短（subject + claim 一行），不用像消息那样截断太狠
        hits = [
            {
                "type": "fact",
                "text": f"{r['subject']}｜{r['claim']}",
                "who": r["subject"],
                "ts": "",
                "quote": (r["quote"] or "")[:HIT_CHARS],
            }
            for r in rows
        ]
        payload = {"query": q, "count": len(hits), "hits": hits}
        if not hits:
            # 模型在 0 结果之后最常见的动作是换个词再调一次。明确告诉它这条路不通，
            # 比在评测里加一条规则更省事（也真的减少了调用次数）。
            payload["note"] = "事实库里没有这条。不要再调 search_facts，请改用 search_memory 找聊天原话。"
        note = {"tool": name, "args": {"query": q, "limit": limit}, "n": len(hits), "mode": "keyword"}
        facts.bump(conn, [r["id"] for r in rows])
    else:
        payload = {"error": f"没有这个工具: {name}"}
        note = {"tool": name, "args": args, "n": 0, "mode": "-"}

    note["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return json.dumps(payload, ensure_ascii=False), note


def _call_key(name: str, args: dict[str, Any]) -> str:
    return name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)


def _run_tool(
    name: str,
    args: Any,
    conn,
    state: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """_run_tool_raw 的安全外壳：去重、限次、兜异常。

    三件事都是评测里真实踩出来的：
    - **去重**：开了规则前置之后，模型紧接着又调一次同名同参数（很常见），
      等于多花一整轮 token 和 1.5 秒；router 的 docstring 早就承诺「不许重复调
      同一次检索」，但一直没有实现。
    - **限次**：`fetch_url` 的描述写着「一次对话最多抓 2 个页面」，之前没有计数器，
      模型能一口气抓四五个（每个都可能触发真浏览器兜底，很贵）。
    - **兜异常**：模型给个坏参数（`{"limit":"五张"}`）以前直接 500，
      把已经花掉的检索和 token 全白费；现在变成一条 error 结果喂回去。
    """
    if not isinstance(args, dict):
        args = {}
    t0 = time.perf_counter()
    key = _call_key(name, args)
    if state is not None:
        if key in state["seen"]:
            note = {"tool": name, "args": args, "n": 0, "mode": "dedup", "ms": 0.0,
                    "note": "重复调用，已跳过"}
            return (
                json.dumps(
                    {"note": "这个检索刚刚做过，结果就在上一条工具消息里，直接用它回答，别再调一次。"},
                    ensure_ascii=False,
                ),
                note,
            )
        state["seen"].add(key)
        if name == "fetch_url":
            state["fetches"] += 1
            if state["fetches"] > 2:
                note = {"tool": name, "args": args, "n": 0, "mode": "limit", "ms": 0.0,
                        "note": "超过 2 页上限"}
                return (
                    json.dumps(
                        {"note": "这次对话已经抓过 2 个页面了，改用已有结果回答，或者直接给链接。"},
                        ensure_ascii=False,
                    ),
                    note,
                )
    try:
        return _run_tool_raw(name, args, conn)
    except Exception as e:  # noqa: BLE001 —— 工具坏不能把整条对话打断
        print(f"[warn] 工具 {name} 失败: {type(e).__name__}: {e}", flush=True)
        note = {"tool": name, "args": args, "n": 0, "mode": "error",
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": f"{type(e).__name__}: {e}"}
        return (
            json.dumps(
                {"error": f"这个工具这次失败了（{type(e).__name__}）。换一种方式，或直接说做不到。"},
                ensure_ascii=False,
            ),
            note,
        )


def _norm_history(history: Any) -> list[dict[str, str]]:
    """把外面传进来的「刚才的对话」洗成干净的 messages 片段。

    为什么要洗：这东西来自 HTTP 参数，可能是任意 JSON。
    只认 {"role", "content"} 两个字段，role 只允许 user/assistant，
    content 超长直接截断——不然一个特制的输入就能把我的上下文撑爆、或者冒充 system。
    """
    if not history:
        return []
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except json.JSONDecodeError:
            return []
    if not isinstance(history, list):
        return []
    out: list[dict[str, str]] = []
    for m in history[-HISTORY_TURNS * 2:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = str(m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        out.append({"role": role, "content": content[:HISTORY_CHARS]})
    return out


def run(conn, question: str, max_rounds: int = MAX_ROUNDS, history: Any = None) -> dict[str, Any]:
    """跑一轮「问 → 模型决定调工具 → 拿结果再回答」，最多 max_rounds 轮工具调用。

    history：刚才的对话，用来解决「刚才那句」「上一条」这类指代。
    单轮接口拿不到上下文时，模型只能瞎猜；给了 history 才有东西可指。
    """
    if not available():
        return {"ok": False, "error": "没配 DEEPSEEK_API_KEY，A2 用不了", "answer": "", "trace": []}

    t0 = time.perf_counter()
    past = _norm_history(history)

    # 先查有没有被纠正过（v0.13）。命中就原样答，不叫模型：
    # 纠正的语义常常很窄，让模型"用自己的话说一遍"它会顺手补编细节，等于没纠成。
    # 放在 available() 之后只是为了保持返回结构一致（没 key 时也照样能答）。
    hit = learn.find(conn, question)
    if hit:
        return {
            "ok": True,
            "question": question,
            "answer": hit["right"],
            "trace": [
                {
                    "tool": "LEARNED",
                    "args": {"id": hit["id"], "question": hit["question"]},
                    "n": 0,
                    "ms": 0,
                    "mode": "-",
                    "hits": [],
                    "note": "这条是用户教过的（hits=%d）" % int(hit.get("hits") or 0),
                }
            ],
            "tool_calls": ["LEARNED"],
            "rounds": 0,
            "prefetch": [],
            "took_ms": round((time.perf_counter() - t0) * 1000, 1),
            "model": "learned",
            "history": len(past),
            "learned": True,
            "learn_id": hit["id"],
        }

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}]
    # 群内词库（A6）：这句话里如果有它自学过的词，直接把解释摆在最前面。
    # 不做成工具：模型得先「知道自己不知道」才会去调，而这里它本来就不知道，
    # 反而不会调。词库是白送的上下文，不是它要争取的东西。
    try:
        lex_hint = lexicon.hint(conn, question) if cfg.LEXICON_HINT else ""
    except Exception:  # noqa: BLE001 —— 词库坏了不该让整个回答挂掉
        lex_hint = ""
    if lex_hint:
        messages.append({"role": "system", "content": lex_hint})
    if past:
        messages.append({"role": "system", "content": "下面是刚才的对话（「刚才那句」「上一条」指的是它）："})
        messages.extend(past)
    messages.append({"role": "user", "content": question})
    trace: list[dict[str, Any]] = []
    answer = ""
    prefetched: list[str] = []
    # 一次对话里的工具调用状态：去重 + 抓页计数（见 _run_tool）
    state: dict[str, Any] = {"seen": set(), "fetches": 0}

    # 规则前置：有把握就先替它把工具跑了，省掉「决定要不要调」那一次往返。
    #
    # 第一版是把结果当成一条 system 消息塞进去（「我先预查了，结果如下，够就别重复查」），
    # 实测 38 条被前置的题，模型**全部又自己调了一遍**——它不是不讲理，是没把这当回事：
    # 在它眼里「还没调过工具」，system 里的字只是参考资料。
    # 所以第二版改成伪造一轮真正的工具往返：assistant(tool_calls) + tool(结果)，
    # 模型看到的状态就是「这工具我刚调过、结果是这个」，才会接着写答案而不是再调一次。
    if PREFETCH:
        plan = router.plan(question, bool(past), rules=router.learned_rules(conn))
        if plan:
            calls: list[dict[str, Any]] = []
            tool_msgs: list[dict[str, Any]] = []
            for idx, item in enumerate(plan, 1):
                cid = f"prefetch_{idx}"
                result, note = _run_tool(item["name"], item["args"], conn, state)
                note["prefetch"] = True
                trace.append(note)
                prefetched.append(item["name"])
                calls.append(
                    {
                        "id": cid,
                        "type": "function",
                        "function": {
                            "name": item["name"],
                            "arguments": json.dumps(item["args"], ensure_ascii=False),
                        },
                    }
                )
                tool_msgs.append({"role": "tool", "tool_call_id": cid, "content": result})
            at = len(messages) - 1
            messages.insert(at, {"role": "assistant", "content": "", "tool_calls": calls})
            for i, m in enumerate(tool_msgs):
                messages.insert(at + 1 + i, m)

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
            result, note = _run_tool(name, args, conn, state)
            trace.append(note)
            messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": result})

    took = round((time.perf_counter() - t0) * 1000, 1)
    names = [t["tool"] for t in trace]
    # 轮数只数「模型自己发起的」工具调用：前置的那些是规则干的，不该算模型的轮次，
    # 否则开了前置看起来像是模型更爱调工具，其实正好相反。
    model_rounds = len([t for t in trace if t["tool"] != "STOP" and not t.get("prefetch")])
    return {
        "ok": True,
        "question": question,
        "answer": answer,
        "trace": trace,
        "tool_calls": names,
        "rounds": model_rounds,
        "prefetch": prefetched,
        "took_ms": took,
        "model": cfg.DEEPSEEK_MODEL,
        "history": len(past),
    }
