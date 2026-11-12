"""把 AstrBot 里最新的群消息同步进 group-brain（服务器上跑）。

为什么需要它：
  以前「更新数据」是这么干的——等有人（其实一直是我）手动 SSH 上去把 data_v4.db 拷下来、
  再在本地全量重导一遍。结果就是：**只要没人手动做，库里的数据就停在最后一次手导的那天**。
  2026-11-12 发现库里还停在 11-05，中间一周的消息一条都没有。

  现在改成服务器自己每天凌晨抓一次：直接读 AstrBot 的**活性库**（不是拷出来的旧文件），
  用 sqlite 的在线备份接口拿一份一致快照（WAL 里的新数据也不会漏），
  增量入库，然后只给新增的消息算向量。

为什么要用 backup API 而不是 `cp`：
  AstrBot 在跑，库是 WAL 模式。`cp` 只拷主库文件，会丢掉 -wal 里还没合并的那部分——
  正好是最新的消息。backup API 会把这些一起带过来。

用法（服务器）：
  /opt/group-brain/.venv/bin/python scripts/sync_messages.py
  ... --src /opt/qqbot/astrbot-data/data_v4.db   # 换数据源
  ... --no-vectors                              # 只入库，先不算向量
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.import_messages import import_from  # noqa: E402

# AstrBot 的库挂在容器的 /AstrBot/data 下，宿主机路径是这个
DEFAULT_SRC = os.environ.get("ASTRBOT_LIVE_DB", "/opt/qqbot/astrbot-data/data_v4.db")
SCRIPTS = ROOT / "scripts"


def snapshot(live: Path) -> Path:
    """把活性库拍一份一致快照到临时文件，返回快照路径。"""
    if not live.exists():
        raise SystemExit(f"[X] 找不到 AstrBot 的库: {live}")
    tmp = Path(tempfile.gettempdir()) / f"gb_sync_{int(time.time())}.db"
    src = sqlite3.connect(f"file:{live}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)  # 会把 WAL 里没合并的部分一起带过来
    finally:
        dst.close()
        src.close()
    return tmp


def main() -> None:
    ap = argparse.ArgumentParser(description="从 AstrBot 活性库增量同步群消息")
    ap.add_argument("--src", default=DEFAULT_SRC, help="AstrBot 的 data_v4.db")
    ap.add_argument("--no-vectors", action="store_true", help="只入库，不算向量")
    args = ap.parse_args()

    src = Path(args.src)
    t0 = time.perf_counter()
    snap = snapshot(src)
    print(f"[ok] 已取快照 {snap.name}（{snap.stat().st_size / 1024:.0f} KB，{time.perf_counter() - t0:.1f}s）", flush=True)

    try:
        res = import_from(snap)
    finally:
        snap.unlink(missing_ok=True)

    print(
        f"[ok] 原始 {res['raw']} 条 → 新增 {res['added']} / 更新 {res['updated']} / "
        f"没变 {res['skipped']}，库里现在 {res['total']} 条",
        flush=True,
    )

    if args.no_vectors or (res["added"] + res["updated"]) == 0:
        if (res["added"] + res["updated"]) == 0:
            print("[ok] 没有新消息，向量也不用算", flush=True)
        return

    print("[..] 给新消息算向量（只算缺的那些）", flush=True)
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "build_vectors.py"), "--kind", "message"],
        cwd=str(ROOT),
    )
    if r.returncode != 0:
        print("[!] 向量没算完（不影响检索：向量那一路会退回关键词，下次跑会接着算）")


if __name__ == "__main__":
    main()
