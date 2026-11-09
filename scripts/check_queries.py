"""检查 eval/queries.jsonl 有没有写坏。

JSONL 是「一行一个 JSON」，VS Code 不会给你标红——少个引号、末尾多个逗号、
中文输入法带出全角引号，肉眼都看不出来。这个脚本专门干这件事：

    .venv\\Scripts\\python.exe scripts/check_queries.py

出错会告诉你第几行、错在哪；没错会打印条数。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import EVAL_DIR

PATH = EVAL_DIR / "queries.jsonl"


def main() -> None:
    if not PATH.exists():
        print(f"[X] 找不到 {PATH}")
        raise SystemExit(1)

    raw = PATH.read_text(encoding="utf-8")
    if raw.startswith("\ufeff"):
        print("[X] 文件开头有 BOM（VS Code 右下角选成了「UTF-8 with BOM」）")
        print("    点右下角的编码 → Save with Encoding → UTF-8，再保存一次")
        raise SystemExit(1)

    errors: list[str] = []
    qs: list[str] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append(f"第 {lineno} 行不是合法 JSON：{e.msg}（第 {e.colno} 个字符附近）")
            errors.append(f"    {line[:80]}")
            continue
        if not isinstance(obj, dict):
            errors.append(f"第 {lineno} 行不是 {{...}} 对象")
            continue
        q, exp = obj.get("q"), obj.get("expect")
        if not isinstance(q, str) or not q.strip():
            errors.append(f'第 {lineno} 行缺 "q" 或 q 是空的')
            continue
        if not isinstance(exp, list) or not exp:
            errors.append(f'第 {lineno} 行 "expect" 必须是非空数组，例：["原文片段"]')
            continue
        if not all(isinstance(x, str) and x.strip() for x in exp):
            errors.append(f"第 {lineno} 行 expect 里有空字符串或非字符串")
            continue
        qs.append(q.strip())

    dup = [q for q, n in Counter(qs).items() if n > 1]
    if dup:
        errors.append(f"有 {len(dup)} 条查询重复了：" + "、".join(dup[:5]))

    if errors:
        print(f"[X] 有 {len(errors)} 个问题：\n")
        for e in errors:
            print(e)
        print("\n改完再跑一次本脚本。看不懂的错误就把行号发我。")
        raise SystemExit(1)

    print(f"[OK] {len(qs)} 条查询，格式全对。")
    print("     下一步：.venv\\Scripts\\python.exe scripts\\run_eval.py")


if __name__ == "__main__":
    main()
