"""路由实验：同一份 50 条 case，四个「决定调哪个工具」的方案谁更划算。

四个方案：
  rule   纯正则：最多 20 行代码，0 元、0 毫秒
  small  小模型（硅基流动免费 Qwen2.5-7B）：0 元、几百毫秒
  big    大模型（deepseek-chat）：按 token 收钱、一两秒
  route  三级路由：规则有把握就规则 → 没把握问小模型 → 小模型也不确定才问大模型

判分只看一件事：**工具集合是否和期望完全相等**。
不判回答好不好——那是另一件事（见 EVAL.md 第七节）。

用法：
  python scripts/bench_routing.py            # 跑全部
  python scripts/bench_routing.py --limit 5  # 先试 5 条
  python scripts/bench_routing.py --arms rule,small
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

CASES = ROOT / "eval" / "agent_cases.jsonl"

# 价格（元 / 百万 token）。硅基流动的 Qwen2.5-7B 在免费池，所以记 0。
PRICE = {
    "small": (0.0, 0.0),
    "big": (2.0, 8.0),  # deepseek-chat 公开价：输入 2 元/M（未命中缓存）、输出 8 元/M
}

SYSTEM = """你在决定：为了回答用户这句话，需要查哪些工具。只能输出 JSON，不要解释。

可用工具：
- search_memory：翻群聊历史记录（谁说过什么、什么时候说过、有没有人提过）
- search_meme：找表情包图片
- get_time：查现在的时间和日期

判断规则：
1. 用户问的是「群里发生过的事、谁说的、什么时候说的」→ search_memory
2. 用户要图 / 要表情包 → search_meme
3. 用户问今天几号、现在几点，或用到「今天/昨天/前天」这类相对时间 → get_time
4. 问机器人自己（你会什么、你是谁、你的设定）、寒暄（你好/谢谢/晚安/哈哈）、
   或明显超出能力（查天气、写代码）→ 一个工具都不要调
5. 一句话里同时需要两种（比如「昨天有人提过原神吗」）→ 两个都调

输出格式：{"tools": ["search_memory"], "why": "一句话"}
不要输出空 why，不要输出多余字段。"""

SCHEMA_HINT = '{"tools": [...], "why": "..."}'


# ---------------------------------------------------------------- 规则方案
GREET = re.compile(r"^(你?好|您好|谢谢|多谢|晚安|早安|早上好|在吗|在不在|哈哈+|嗯+|好的|收到|ok)[呀啊哈吗呢~！!。.？?]*$", re.I)
MEME = re.compile(r"(表情|表情包|贴切|来张图|来张|一张图|给张图|发张|图$|gif)", re.I)
TIME = re.compile(r"(今天|昨天|前天|明天|现在|几号|几点|星期几|周几|日期|时间|今年|哪年|几月)")
REL = re.compile(r"(今天|昨天|前天|这两天|最近)")
# 纯问时间（「今天几号」「今天星期几」）只需要 get_time，不需要翻聊天记录。
# 不单独处理的话，「今天」这个 REL 会顺手把 search_memory 也塞进去。
PURE_TIME = re.compile(r"^(今天|昨天|明天|现在|这周|本周)?\s*(几号|几点了?|几点|星期几|周几|什么日子|哪一年|哪年|几月几号|日期|时间)[呀啊呢?？]*$")
MEMORY = re.compile(r"(谁|哪句|哪条|哪个|说过|提过|提过吗|聊过|聊了什么|有没有人|有人|叫啥|叫什么|什么时候|那次|上次|原话|说过吗|啥时候)")
# 指代词：规则天生处理不了「刚才那句」「上一条」「他」——这类必须交给模型，
# 所以它们不是「高置信」，而是要降级（route 方案的分级依据）。
DEIXIS = re.compile(r"(刚才那|上一条|那句话|那句|这张|那张|那个人|他|她)")


def arm_rule(q: str) -> tuple[list[str], str, float]:
    """返回 (工具集合, 置信度 high/low, 说明)。置信度低的路由方案要去问小模型。"""
    s = q.strip()
    if GREET.match(s):
        return [], "high", "寒暄短句"
    tools: list[str] = []
    if MEME.search(s):
        tools.append("search_meme")
    if TIME.search(s):
        tools.append("get_time")
    # 纯问时间（「今天几号」「今年是哪年」）就别再翻库了
    if PURE_TIME.match(s):
        return tools, "high", "纯问时间"
    if MEMORY.search(s) or (REL.search(s) and not tools):
        if "search_memory" not in tools:
            tools.append("search_memory")
    # 只有「图」没有任何记忆线索时，不要再塞 search_memory
    if tools == ["search_meme"] and MEMORY.search(s):
        tools.append("search_memory")
    # 置信度：命中了某条线索算 high；什么都没命中算 low（可能是「问机器人自己」，
    # 也可能是真该搜但用词很怪——这两种规则分不清，交给小模型）
    if DEIXIS.search(s) and tools:
        return tools, "low", "含指代词，规则判不了"
    if tools:
        return tools, "high", "规则命中"
    if len(s) <= 8 or re.search(r"(你|您)", s):
        return [], "high", "短句/在问机器人自己"
    return [], "low", "规则没命中"


# ---------------------------------------------------------------- 模型方案
def _chat(url: str, key: str, model: str, q: str, timeout: int = 60) -> tuple[str, dict]:
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"用户这句话：{q}\n只输出 JSON，格式 {SCHEMA_HINT}"},
            ],
            "temperature": 0,
            "max_tokens": 120,
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"], data.get("usage", {}) or {}
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"调用失败: {last}")


def _parse_tools(text: str) -> list[str] | None:
    """从模型输出里抠出 tools 数组；抠不出来返回 None（表示「这个方案这次没给出答案」）。"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    tools = obj.get("tools")
    if not isinstance(tools, list):
        return None
    ok = {"search_memory", "search_meme", "get_time"}
    return [t for t in tools if t in ok]


def arm_small(q: str) -> tuple[list[str] | None, dict, float]:
    t0 = time.time()
    text, usage = _chat(config.SILICONFLOW_URL, config.SILICONFLOW_KEY, config.SMALL_MODEL, q)
    return _parse_tools(text), usage, (time.time() - t0) * 1000


def arm_big(q: str) -> tuple[list[str] | None, dict, float]:
    t0 = time.time()
    text, usage = _chat(config.DEEPSEEK_URL, config.DEEPSEEK_KEY, config.DEEPSEEK_MODEL, q)
    return _parse_tools(text), usage, (time.time() - t0) * 1000


def arm_route(q: str) -> tuple[list[str] | None, dict, float, str]:
    """规则 → 小模型 → 大模型，逐级降级。返回 (tools, usage, ms, 走了哪一级)。"""
    t0 = time.time()
    tools, conf, why = arm_rule(q)
    if conf == "high":
        return tools, {}, (time.time() - t0) * 1000, "rule"
    try:
        tools_s, usage_s, _ = arm_small(q)
        if tools_s is not None:
            return tools_s, usage_s, (time.time() - t0) * 1000, "small"
    except RuntimeError:
        usage_s = {}
    tools_b, usage_b, _ = arm_big(q)
    return tools_b, usage_b, (time.time() - t0) * 1000, "big"


# ---------------------------------------------------------------- 主流程
def load_cases(limit: int | None, path: Path | None = None) -> list[dict]:
    cases = []
    for line in (path or CASES).read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases[:limit] if limit else cases


def cost_of(usage: dict, arm: str) -> float:
    pin, pout = PRICE.get(arm, (0.0, 0.0))
    return (usage.get("prompt_tokens", 0) * pin + usage.get("completion_tokens", 0) * pout) / 1_000_000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--arms", default="rule,small,big,route")
    ap.add_argument("--cases", default="", help="换一份 case 文件（比如 --cases eval/agent_holdout.jsonl 做留出集）")
    ap.add_argument("--show", type=int, default=8, help="打印前 N 条判错的细节")
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    path = Path(args.cases) if args.cases else None
    cases = load_cases(args.limit or None, path)
    print(f"共 {len(cases)} 条 case（{path.name if path else '<默认:agent_cases>'}），方案：{', '.join(arms)}\n")

    summary: dict[str, dict] = {}
    for arm in arms:
        ok = 0
        ms_all: list[float] = []
        cost_all = 0.0
        route_stats: dict[str, int] = {}
        bad: list[tuple[str, list[str], list[str]]] = []
        for c in cases:
            q = c["q"]
            want = sorted(c["tools"])
            try:
                if arm == "rule":
                    got, conf, why = arm_rule(q)
                    ms = 0.0
                    usage: dict = {}
                elif arm == "small":
                    got, usage, ms = arm_small(q)
                elif arm == "big":
                    got, usage, ms = arm_big(q)
                elif arm == "route":
                    got, usage, ms, level = arm_route(q)
                    route_stats[level] = route_stats.get(level, 0) + 1
                else:
                    raise SystemExit(f"未知方案 {arm}")
            except RuntimeError as e:
                got, usage, ms = None, {}, 0.0
                print(f"  [{arm}] 调用失败：{q} → {e}")
            ms_all.append(ms)
            cost_all += cost_of(usage or {}, "big" if arm in ("big", "route") and usage else arm)
            if got is not None and sorted(got) == want:
                ok += 1
            else:
                bad.append((q, want, got or ["<没解析出来>"]))
        n = len(cases)
        summary[arm] = {
            "正确": ok,
            "正确率": ok / n if n else 0,
            "平均延迟": sum(ms_all) / n if n else 0,
            "成本": cost_all,
            "每千次成本": cost_all / n * 1000 if n else 0,
            "route 分级": route_stats,
            "错的": bad,
        }

    print("=" * 74)
    print(f"{'方案':<8}{'正确率':>10}{'平均延迟':>12}{'跑完这次花':>14}{'每千次':>12}")
    print("-" * 74)
    for arm in arms:
        s = summary[arm]
        print(f"{arm:<8}{s['正确']:>4}/{len(cases):<4}{s['平均延迟']:>10.0f}ms{s['成本']:>12.6f}元{s['每千次成本']:>10.4f}元")
    print("=" * 74)

    for arm in arms:
        s = summary[arm]
        if s["route 分级"]:
            print(f"[{arm}] 分级：{s['route 分级']}")
        if s["错的"]:
            print(f"\n[{arm}] 错了 {len(s['错的'])} 条，前 {min(args.show, len(s['错的']))} 条：")
            for q, want, got in s["错的"][: args.show]:
                print(f"  · {q}")
                print(f"     期望 {want}  实际 {got}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
