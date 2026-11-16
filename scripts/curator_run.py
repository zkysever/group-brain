"""手动跑一轮 B 站选片（面板按钮 / cron 备用入口）。

用法：
    python scripts/curator_run.py            # 正常一轮（受窗口与每小时限额约束）
    python scripts/curator_run.py --force    # 无视窗口与「这一小时推过」
    python scripts/curator_run.py --dry      # 只判断不写 outbox、不推
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import curator, db  # noqa: E402


def main() -> int:
    res = curator.run(db.connect(), force="--force" in sys.argv, dry="--dry" in sys.argv)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
