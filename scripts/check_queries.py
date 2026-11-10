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
    # 不传参数就查默认那份（检索评测集）；传了就按顺序逐个查
    # （CI 里会把检索集、Agent 集、上下文题三份一起过一遍）。
    paths = [Path(a) for a in sys.argv[1:]] or [PATH]
    bad = 0
    for p in paths:
        # 允许三种写法：绝对路径、相对当前目录（eval/xxx.jsonl）、只给文件名（xxx.jsonl）
        if not p.is_absolute() and not p.exists():
            p = EVAL_DIR / p.name
        bad += _check_one(p)
    if bad:
        raise SystemExit(1)


def _check_one(PATH: Path) -> int:
    """查一份 JSONL，返回问题数量（0 = 干净）。"""
    print(f"—— {PATH.name} ——")
    if not PATH.exists():
        print(f"[X] 找不到 {PATH}")
        return 1

    raw = PATH.read_text(encoding="utf-8")
    if raw.startswith("\ufeff"):
        print("[X] 文件开头有 BOM（VS Code 右下角选成了「UTF-8 with BOM」）")
        print("    点右下角的编码 → Save with Encoding → UTF-8，再保存一次")
        return 1

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
        q = obj.get("q")
        if not isinstance(q, str) or not q.strip():
            errors.append(f'第 {lineno} 行缺 "q" 或 q 是空的')
            continue

        # 两种题集两种字段：
        #   检索评测 → "expect": ["原文片段", ...]（必须非空）
        #   Agent 评测 → "tools": ["search_memory", ...]（可以为空 = 不该调）
        if "expect" in obj:
            exp = obj["expect"]
            if not isinstance(exp, list) or not exp:
                errors.append(f'第 {lineno} 行 "expect" 必须是非空数组，例：["原文片段"]')
                continue
            if not all(isinstance(x, str) and x.strip() for x in exp):
                errors.append(f"第 {lineno} 行 expect 里有空字符串或非字符串")
                continue
        elif "tools" in obj:
            tools = obj["tools"]
            if not isinstance(tools, list):
                errors.append(f'第 {lineno} 行 "tools" 必须是数组（不该调工具就写 []）')
                continue
            if not all(isinstance(x, str) and x.strip() for x in tools):
                errors.append(f"第 {lineno} 行 tools 里有空字符串或非字符串")
                continue
        else:
            errors.append(f'第 {lineno} 行既没有 "expect" 也没有 "tools"，不知道该怎么判分')
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
        return len(errors)

    print(f"[OK] {len(qs)} 条，格式全对。")
    return 0


if __name__ == "__main__":
    main()
