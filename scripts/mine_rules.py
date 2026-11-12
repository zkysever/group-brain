"""从评测集里挖「某种问法 → 该调哪个工具」的规则（v0.15）。

为什么从评测集挖、不从群聊挖：
- 群聊里没有「正确答案」这个字段，只有人说过什么。要挖规则，得先知道每种问法
  **应该**怎么处理——那正是评测集在做的事。
- 评测集有 50 条主集 + 10 条留出集。主集挖规则、留出集当考试题，
  这样「规则有没有用」不是我说了算，是留出集上不掉分说了算。

三道闸门（缺一不写库）：
1. 支持度：这条规则在训练集里至少命中 `MIN_HIT` 次；
2. 纯度：命中它的题里，`precision` 必须够高（错一次就否掉，因为前置错了
   就是把错误答案直接喂给模型，比慢更糟）；
3. 影子测试：拿规则路由去跑留出集，准确率**不许低于**内置规则（`arm_rule`），
   且要么准确率涨、要么前置率涨（否则没有意义）。

写进库时 `enabled=0`，人工在 /admin 或 /rules 里点头才生效——
和事实层一个道理：模型挖出来的东西不能自己给自己发许可证。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, facts  # noqa: E402
from app import router as rt      # noqa: E402

MIN_HIT = 2          # 至少命中 2 条
MIN_PREC = 0.99      # 纯度：命中它的题里不能有一条是别的答案
NGRAM = (2, 3, 4)    # 字符 n-gram 长度（中文 2–4 字就够表达「几点」「谁说过」）


def load_cases(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        tools = c.get("tools")
        if tools is None:
            continue
        out.append({"q": c["q"], "tools": tuple(sorted(tools))})
    return out


def ngrams(text: str) -> set[str]:
    s = "".join(ch for ch in text if ch.strip())
    out: set[str] = set()
    for n in NGRAM:
        for i in range(len(s) - n + 1):
            out.add(s[i : i + n])
    return out


def mine(cases: list[dict]) -> list[dict]:
    """给每个 (ngram, tools) 记支持度和纯度，取纯的出来。"""
    stat: dict[tuple[str, tuple[str, ...]], list[int]] = defaultdict(lambda: [0, 0])
    for c in cases:
        for g in ngrams(c["q"]):
            stat[(g, c["tools"])][0] += 1
    # 再数「含 g 但答案不是这组 tools」的题，算纯度
    bad: dict[str, int] = defaultdict(int)
    by_gram: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for c in cases:
        for g in ngrams(c["q"]):
            by_gram[g].append(c["tools"])
    rules = []
    for (g, tools), (hit, _) in stat.items():
        total = len(by_gram.get(g, []))
        if hit < MIN_HIT or total == 0:
            continue
        prec = hit / total
        if prec < MIN_PREC:
            continue
        rules.append({"pattern": g, "tools": list(tools), "evidence": hit, "precision": round(prec, 3)})
    # 同一 tools 下留最长的那条（「几点」比「点」具体），并按支持度排序
    best: dict[tuple[str, ...], dict] = {}
    for r in rules:
        t = tuple(r["tools"])
        if t not in best or len(r["pattern"]) > len(best[t]["pattern"]):
            best[t] = r
    return sorted(best.values(), key=lambda r: (-len(r["pattern"]), -r["evidence"]))


def shadow(rules: list[dict], cases: list[dict]) -> dict:
    """影子测试：把规则当路由，跑一遍留出集，和内置规则比。"""
    def route(q: str) -> tuple[tuple[str, ...], str]:
        flat = "".join(ch for ch in q if ch.strip())
        for r in rules:
            if r["pattern"] in flat:
                return tuple(r["tools"]), "rule"
        tools, conf, _why = rt.rule_route(q)
        return tuple(tools), conf

    base_ok = new_ok = 0
    base_pre = new_pre = 0
    detail = []
    for c in cases:
        b_tools, b_conf = rt.rule_route(c["q"])[:2]
        n_tools, _ = route(c["q"])
        b_hit = tuple(sorted(b_tools)) == c["tools"]
        n_hit = tuple(sorted(n_tools)) == c["tools"]
        base_ok += b_hit
        new_ok += n_hit
        # 内置规则只在 conf=high 且非空时才真的会前置
        if b_tools and b_conf == "high":
            base_pre += 1
        if n_tools:
            new_pre += 1
        if b_hit != n_hit:
            detail.append({"q": c["q"], "want": list(c["tools"]),
                           "rule": list(b_tools), "new": list(n_tools),
                           "better": n_hit and not b_hit})
    n = len(cases) or 1
    return {
        "n": len(cases),
        "base_ok": base_ok, "new_ok": new_ok,
        "base_pre": base_pre, "new_pre": new_pre,
        "base_acc": round(base_ok / n, 3), "new_acc": round(new_ok / n, 3),
        "detail": detail,
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="从评测集挖工具选择规则，留出集影子测试后写进 rules 表")
    ap.add_argument("--train", default=str(ROOT / "eval" / "agent_cases.jsonl"))
    ap.add_argument("--holdout", default=str(ROOT / "eval" / "agent_holdout.jsonl"))
    ap.add_argument("--write", action="store_true", help="真的写库（默认只打印）")
    ap.add_argument("--enable", action="store_true", help="写库时直接 enabled=1（默认 0，等人点头）")
    args = ap.parse_args()

    train = load_cases(Path(args.train))
    hold = load_cases(Path(args.holdout))
    print(f"[读] 训练集 {len(train)} 条，留出集 {len(hold)} 条")

    rules = mine(train)
    print(f"[挖] 通过支持度+纯度的候选 {len(rules)} 条：")
    for r in rules:
        print(f"     {r['pattern']!r} → {r['tools']}  支持 {r['evidence']}，纯度 {r['precision']}")

    if not rules:
        print("[停] 没有候选，不写库")
        return

    s = shadow(rules, hold)
    print(f"\n[影子测试·留出集] 内置规则 {s['base_ok']}/{s['n']} = {s['base_acc']}（会前置 {s['base_pre']} 条）"
          f" → 加规则后 {s['new_ok']}/{s['n']} = {s['new_acc']}（会前置 {s['new_pre']} 条）")
    for d in s["detail"]:
        print(f"     {'✅' if d['better'] else '❌'} {d['q']}  应为 {d['want']}  原 {d['rule']} → 新 {d['new']}")

    gate_ok = (s["new_acc"] >= s["base_acc"]) and (s["new_acc"] > s["base_acc"] or s["new_pre"] > s["base_pre"])
    print(f"\n[闸门] 准确率不掉 + (准确率或前置率有提升) = {gate_ok}")
    if not gate_ok:
        print("[停] 闸门没过，不写库")
        return

    if not args.write:
        print("[试跑] 没加 --write，不写库")
        return

    conn = db.connect()
    n = 0
    for r in rules:
        conn.execute(
            "INSERT INTO rules (pattern, tool, evidence, precision, note, enabled, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))",
            (r["pattern"], ",".join(r["tools"]), r["evidence"], r["precision"],
             f"从评测集挖出，留出集 {s['new_acc']}/{s['n']}", 1 if args.enable else 0),
        )
        n += 1
    conn.commit()
    print(f"[写库] {n} 条，enabled={1 if args.enable else 0}；/admin 里能看到，点头后参与前置")
    conn.close()


if __name__ == "__main__":
    main()
