"""A2 评测：只看「它有没有在该调工具的时候调、不该调的时候住手」。

不看回答漂不漂亮 —— 那得人来看。机器能判的只有三件事：
  1. 该调的调了没有（漏调）
  2. 不该调的有没有乱调（误触发，最恶心的一种，又慢又烧钱）
  3. 调了几次（超过 max_calls 就算过度调用）

用法：
  python scripts/run_agent_eval.py                 # 全跑
  python scripts/run_agent_eval.py --limit 5       # 先跑 5 条看看
  python scripts/run_agent_eval.py --slow 0.5      # 每条之间歇 0.5 秒

一句话：偷懒的评测比没有评测更坏，所以这里只测能自动判的东西。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, db  # noqa: E402
from app.config import EVAL_DIR  # noqa: E402

CASES = EVAL_DIR / "agent_cases.jsonl"  # 默认；--cases 可以换一份


def load_cases(limit: int | None, grep: str = "", path: Path | None = None) -> list[dict]:
    rows = []
    for i, line in enumerate((path or CASES).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise SystemExit(f"agent_cases.jsonl 第 {i} 行不是合法 JSON: {e}")
    if grep:
        rows = [r for r in rows if grep in r["q"]]
    return rows[:limit] if limit else rows


def judge(case: dict, result: dict) -> tuple[bool, str]:
    """返回 (这条算不算对, 一句话原因)。

    两件事分开判：
      1. 工具集合对不对（该调的调了、不该调的住手了）
      2. 如果 case 里写了 expect_any，回答里必须出现其中**任意一个**关键词。
         因为「工具集合对了」不等于「答对了」：模型可以一个工具都不调，
         然后回一句「我不知道你在说什么」——工具层面完全正确，答案是废的。
    """
    want = list(case.get("tools") or [])
    got = [t for t in (result.get("tool_calls") or []) if t != "STOP"]
    max_calls = int(case.get("max_calls", len(want)))

    if not result.get("ok"):
        return False, "调用失败: " + str(result.get("error"))[:80]

    if want and not got:
        return False, "该调没调"
    if not want and got:
        return False, "不该调却调了 " + "/".join(got)
    extra = [t for t in got if t not in want]
    if want and extra:
        return False, "多调了 " + "/".join(extra)
    if len(got) > max_calls:
        return False, f"调了 {len(got)} 次，超过上限 {max_calls}"

    expect_any = case.get("expect_any") or []
    if expect_any:
        answer = str(result.get("answer") or "")
        if not any(k in answer for k in expect_any):
            return False, "答非所问（回答里没出现 " + " / ".join(expect_any[:3]) + "）"
    return True, "OK"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--grep", default="", help="只跑问题里含这个子串的）")
    ap.add_argument("--slow", type=float, default=0.3)
    ap.add_argument("--cases", default="", help="换一份 case 文件，比如 eval/agent_history_cases.jsonl")
    ap.add_argument("--no-history", action="store_true", help="忽略 case 里的 history，用来做「不给上下文」的对照")
    ap.add_argument("--no-prefetch", action="store_true", help="关掉规则前置，用来做 A/B 对比（默认是开的）")
    args = ap.parse_args()

    if not agent.available():
        raise SystemExit("没配 DEEPSEEK_API_KEY，跑不了")

    if args.no_prefetch:
        agent.PREFETCH = False

    cases = load_cases(args.limit or None, args.grep, Path(args.cases) if args.cases else None)
    conn = db.connect()
    rows = []
    t0 = time.time()

    print(f"跑 {len(cases)} 条（模型 {agent.cfg.DEEPSEEK_MODEL}，规则前置 {'关' if args.no_prefetch else '开'}）\n")
    for i, c in enumerate(cases, 1):
        hist = None if args.no_history else c.get("history")
        r = agent.run(conn, c["q"], history=hist)
        ok, why = judge(c, r)
        rows.append((c, r, ok, why))
        mark = "OK " if ok else "XX "
        got = "/".join(t for t in (r.get("tool_calls") or []) if t != "STOP") or "-"
        pre = "+".join(r.get("prefetch") or []) or "-"
        print(f"{mark}{i:2d}. {c['q']:<18} 期望={','.join(c['tools']) or '(不调)':<16} 实际={got:<32} 前置={pre:<14} {why}")
        if args.slow:
            time.sleep(args.slow)

    total = len(rows)
    passed = sum(1 for _, _, ok, _ in rows if ok)
    # 误触发 = 本来不该调工具、它却调了
    zero = [x for x in rows if not (x[0].get("tools") or [])]
    over = [x for x in zero if x[2] is False and "不该调" in x[3]]
    rounds = [len([t for t in (r.get("tool_calls") or []) if t != "STOP"]) for _, r, _, _ in rows]
    took = [r.get("took_ms") or 0 for _, r, _, _ in rows]

    print("\n" + "-" * 60)
    print(f"正确率      {passed}/{total} = {passed / total:.3f}")
    if zero:
        print(f"误触发率    {len(over)}/{len(zero)} = {len(over) / len(zero):.3f}   （寒暄类里乱调工具的比例）")
    print(f"平均轮数    {sum(rounds) / total:.2f}")
    print(f"平均耗时    {sum(took) / total:.0f}ms")
    print(f"总耗时      {time.time() - t0:.0f}s")

    bad = [x for x in rows if not x[2]]
    if bad:
        print("\n没过的：")
        for c, r, ok, why in bad:
            print(f"  - {c['q']}：{why}")

    conn.close()

    # 只跑子集时别判定达标，否则 1/1 会得出荒谬结论
    if args.grep or args.limit or args.cases:
        print("\n（子集或非默认题集，不套用 50 条的达标线）")
        return 0

    # A2 的验收线：≥24/30，误触发 <15%
    ok_line = passed >= 24 and (not zero or len(over) / len(zero) < 0.15)
    print("\n" + ("达标" if ok_line else "没达标") + "（线：正确率 ≥24/30 且误触发 <15%）")
    return 0 if ok_line else 1


if __name__ == "__main__":
    raise SystemExit(main())
