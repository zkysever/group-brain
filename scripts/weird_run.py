#!/usr/bin/env python3
"""抽象猎奇视频日报：每天 12:00 推 10–20 条链接。

    python scripts/weird_run.py            # 正常一轮（今天推过就跳过）
    python scripts/weird_run.py --force    # 无视「今天推过」
    python scripts/weird_run.py --dry      # 只挑不发，打印选中了什么

cron 里跑的是不带参数的版本，日志在 /var/log/gb-weird.log。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app import db, weird  # noqa: E402


def main() -> int:
    res = weird.run(db.connect(), force="--force" in sys.argv, dry="--dry" in sys.argv)
    print(json.dumps(res, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
