"""规则前置：一句话就能看出来该查什么，就别再花一次模型往返去问「要不要查」。

这是 A3（`scripts/bench_routing.py`）的结论落进产品里。

A3 当时量出来的是：20 行正则和 deepseek 打平（都是 94%），但便宜 18 倍、快 50 倍。
可那只是「判断该调哪个工具」这一步。真正贵的是**后面那次模型往返**——
先问模型「你打算查什么」，等它开口，再执行工具，再把结果喂回去让它写答案。
一句话要两次往返，一次 1.5~2 秒，还各算一遍 prompt。

规则前置省掉的是第一次往返：既然规则已经有把握，就先把工具跑了，
把结果直接塞进上下文，模型第一次开口就能写答案。

三个前提，缺一个就不前置：
  1. 规则有把握（`conf == "high"`）——low 的情况包括指代和怪用词，正是规则的短板；
  2. 没有带 history——带上下文时「刚才那句」只能靠模型看上文去解，规则一律判 low 早已防住，
     这里再挡一层，是因为 history 存在时任何检索都可能检索错对象；
  3. 规则确实要调工具——寒暄类工具集为空，没有可前置的东西。

设计上刻意只做「预查」，不做「替模型决定」：工具照样给模型，
它觉得预查不够就可以再调，只是不许重复调同一次检索。
"""
from __future__ import annotations

import re
from typing import Any

GREET = re.compile(
    r"^(你?好|您好|谢谢|多谢|晚安|早安|早上好|在吗|在不在|哈哈+|嗯+|好的|收到|ok)[呀啊哈吗呢~！!。.？?]*$",
    re.I,
)
MEME = re.compile(r"(表情|表情包|贴切|来张图|来张|一张图|给张图|发张|图$|gif)", re.I)
TIME = re.compile(r"(今天|昨天|前天|明天|现在|几号|几点|星期几|周几|日期|时间|今年|哪年|几月)")
REL = re.compile(r"(今天|昨天|前天|这两天|最近)")
# 纯问时间（「今天几号」）只要 get_time。不单独处理的话，REL 里的「今天」
# 会顺手把 search_memory 也塞进去，白查一次库。
PURE_TIME = re.compile(
    r"^(今天|昨天|明天|现在|这周|本周)?\s*(几号|几点了?|几点|星期几|周几|什么日子|哪一年|哪年|几月几号|日期|时间)[呀啊呢?？]*$"
)
MEMORY = re.compile(
    r"(谁|哪句|哪条|哪个|说过|提过|提过吗|聊过|聊了什么|有没有人|有人|叫啥|叫什么|什么时候|那次|上次|原话|说过吗|啥时候)"
)
# 指代词：规则天生处理不了「刚才那句」「上一条」，这类必须降级给模型。
DEIXIS = re.compile(r"(刚才那|上一条|那句话|那句|这张|那张|那个人|他|她)")


def learned_rules(conn) -> list[tuple[str, list[str]]]:
    """读「人工点头过」的自学规则（rules 表 enabled=1）。

    这些规则由 `scripts/mine_rules.py` 从评测集里挖出来，通过了留出集影子测试，
    再经人点头才 enabled=1。表不存在或读失败就当没有——规则是加分项，不该让主流程挂。
    """
    try:
        rows = conn.execute(
            "SELECT pattern, tool FROM rules WHERE enabled = 1 ORDER BY evidence DESC, length(pattern) DESC"
        ).fetchall()
    except Exception:                            # noqa: BLE001
        return []
    out = []
    for r in rows:
        tools = [t for t in (r["tool"] or "").split(",") if t]
        if r["pattern"] and tools:
            out.append((r["pattern"], tools))
    return out


def _flat(q: str) -> str:
    return "".join(ch for ch in q if ch.strip())


def rule_route(
    q: str, rules: list[tuple[str, list[str]]] | None = None
) -> tuple[list[str], str, str]:
    """返回 (工具集合, 置信度 high/low, 说明)。

    置信度只用来决定「要不要前置预查」，不影响工具本身：
    就算 low，模型照样能自己调。

    `rules` 是自学规则（`learned_rules` 读出来的）。**先查它们**，
    因为它们比这里手写的正则更具体（是从真实问法里挖出来的 n-gram）；
    但含指代词时不采信——规则天生处理不了「刚才那句」，这是 A3 就量出来的结论。
    """
    s = (q or "").strip()
    if not s:
        return [], "low", "空问题"
    if GREET.match(s):
        return [], "high", "寒暄短句"

    if rules and not DEIXIS.search(s):
        flat = _flat(s)
        for pattern, tools in rules:
            if pattern and pattern in flat:
                return list(tools), "high", f"自学规则「{pattern}」"

    tools: list[str] = []
    if MEME.search(s):
        tools.append("search_meme")
    if TIME.search(s):
        tools.append("get_time")
    if PURE_TIME.match(s):
        return tools, "high", "纯问时间"

    if MEMORY.search(s) or (REL.search(s) and not tools):
        if "search_memory" not in tools:
            tools.append("search_memory")
    # 「来张生气的图」只有图没有记忆线索，别硬塞 search_memory
    if tools == ["search_meme"] and MEMORY.search(s):
        tools.append("search_memory")

    if DEIXIS.search(s) and tools:
        return tools, "low", "含指代词，规则判不了"
    if tools:
        return tools, "high", "规则命中"
    if len(s) <= 8 or re.search(r"(你|您)", s):
        return [], "high", "短句/在问机器人自己"
    return [], "low", "规则没命中"


def plan(
    q: str,
    has_history: bool = False,
    rules: list[tuple[str, list[str]]] | None = None,
) -> list[dict[str, Any]]:
    """该前置哪些工具调用。返回空列表表示「别前置，交给模型」。"""
    if has_history:
        return []
    tools, conf, why = rule_route(q, rules=rules)
    if conf != "high" or not tools:
        return []

    query = (q or "").strip()
    out: list[dict[str, Any]] = []
    for name in tools:
        if name == "search_memory":
            out.append({"name": name, "args": {"query": query, "limit": 5}, "why": why})
        elif name == "search_meme":
            out.append({"name": name, "args": {"query": query, "limit": 3}, "why": why})
        elif name == "get_time":
            out.append({"name": name, "args": {}, "why": why})
    return out
