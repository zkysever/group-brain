"""v0.7 管理面板的后端：在网页上点按钮 = 在服务器上跑一次脚本。

为什么不直接用 shell：登上服务器敲命令这件事，只有我会；你需要能自己重跑。
所以就一层薄薄的封装——每个按钮对应 `scripts/` 里的一个脚本，原样不动。

三条硬性约束（都是踩过的）：
1. **同一时间只允许一个任务**。两个 `build_vectors.py` 同时跑会互相覆盖向量表，
   而且都在调同一个硅基流动接口，纯属自己跟自己抢额度。
2. **必须能看输出**。失败了不给日志的按钮，等于没做——你还得再来问我。
3. **不能阻塞接口**。重建向量要 10 秒、跑评测要 30 秒，用后台线程 + 前端轮询。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import DB_PATH, MEME_PACKS_DIR

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
RAW_DIR = ROOT / "data" / "raw"

# 界面上每个按钮对应这里的一项。argv 里第二个元素之后是脚本参数。
TASKS: dict[str, dict] = {
    "sync": {
        "label": "同步群消息（增量）",
        "script": "sync_messages.py",
        "args": [],
        "cost": "约 5–30 秒，只处理新消息",
        "note": "从 AstrBot 的活性库取一份快照，只导新增的消息，再给新消息算向量。"
                "每天 04:00 会自动跑一次，这里是手动补一次用。",
        "timeout": 600,
    },
    "facts": {
        "label": "抽取事实草稿（会调接口）",
        "script": "extract_facts.py",
        "args": [],
        "cost": "约 3 秒 / 20 条消息，几毛钱",
        "note": "从群聊里抽「谁是什么样」的草稿，写进待审列表。"
                "抽完去 /facts 一条条点头——不点头的草稿检索不到，也不会影响任何回答。",
        "timeout": 900,
    },
    "fts": {
        "label": "重建关键词索引",
        "script": "rebuild_fts.py",
        "args": [],
        "cost": "约 1 秒，不动原始数据",
        "note": "改了分词规则之后必须点这个，否则索引和查询用的不是同一套切法。",
        "timeout": 120,
    },
    "memes": {
        "label": "重新导入表情包",
        "script": "import_memes.py",
        "args": [],
        "cost": "约 2 秒，要读 meme-packs 目录",
        "note": "往 pack 里加了新图之后点这个。会先清空表情包表再灌。",
        "timeout": 300,
    },
    "vectors": {
        "label": "重建向量（会调接口）",
        "script": "build_vectors.py",
        "args": ["--force"],
        "cost": "约 10 秒，605 条，花一点免费额度",
        "note": "表情包描述改了、或者换了 embedding 模型才需要点。平时不用。",
        "timeout": 600,
    },
    "wal": {
        "label": "把 WAL 合并回主库",
        "script": "db_maintenance.py",
        "args": [],
        "cost": "约 1 秒",
        "note": "只是为了文件好看点，数据一条不少。跑完 -wal 会变小。",
        "timeout": 120,
    },
    "vacuum": {
        "label": "压缩数据库（VACUUM）",
        "script": "db_maintenance.py",
        "args": ["--vacuum"],
        "cost": "约 2 秒，期间锁库",
        "note": "删过数据、想还回磁盘空间才用。会短暂锁库，别在有人用时点。",
        "timeout": 300,
    },
}

JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()          # 全局单任务闸门
_OUT_CAP = 8000                   # 日志最多留这么多字符，防止一把把内存吃光


def raw_available() -> bool:
    """服务器上有没有 AstrBot 原始库——没有就不能在线上重新导入聊天记录。"""
    try:
        return any(RAW_DIR.glob("*.db")) if RAW_DIR.exists() else False
    except OSError:
        return False


def _run(name: str, script: str, args: list[str], timeout: int) -> None:
    job = JOBS[name]
    argv = [sys.executable, str(SCRIPTS / script), *args]
    job.update({"status": "running", "started": time.time(), "finished": None, "rc": None, "out": ""})
    # 子进程强制 UTF-8：Windows 上默认 GBK，中文输出会变成乱码塞进日志
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        p = subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if (p.stderr or "").strip() else "")
        job.update({"rc": p.returncode, "status": "ok" if p.returncode == 0 else "fail", "out": out})
    except subprocess.TimeoutExpired:
        job.update({"rc": -1, "status": "fail", "out": f"[超时] 超过 {timeout} 秒，已放弃"})
    except Exception as e:                       # noqa: BLE001 - 面板宁可显示异常也别白屏
        job.update({"rc": -1, "status": "fail", "out": f"[异常] {type(e).__name__}: {e}"})
    finally:
        job["out"] = (job.get("out") or "")[-_OUT_CAP:]
        job["finished"] = time.time()
        _LOCK.release()


def start(name: str) -> dict:
    task = TASKS.get(name)
    if task is None:
        return {"ok": False, "error": f"没有这个任务: {name}"}
    if not _LOCK.acquire(blocking=False):
        running = [k for k, v in JOBS.items() if v.get("status") == "running"]
        return {"ok": False, "error": f"已经有一个任务在跑（{', '.join(running) or '未知'}），等它完"}

    JOBS[name] = {"status": "running", "started": time.time(), "finished": None, "rc": None, "out": ""}
    t = threading.Thread(target=_run, args=(name, task["script"], task["args"], task["timeout"]), daemon=True)
    t.start()
    return {"ok": True, "job": name}


def _memory_stats(conn) -> dict:
    """A2 的对话记忆有多少。表可能还不存在（老库），所以整个函数都不能抛。"""
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT session_id) s, COUNT(*) n FROM agent_turns"
        ).fetchone()
        return {"sessions": int(row["s"] or 0), "turns": int(row["n"] or 0) // 2}
    except Exception:                            # noqa: BLE001
        return {"sessions": 0, "turns": 0}


def _facts_stats(conn) -> dict:
    """事实记忆：待审 / 生效 / 已拒。同样不能让老库把面板整个打挂。"""
    try:
        rows = conn.execute("SELECT status, COUNT(*) n FROM facts GROUP BY status").fetchall()
        d = {r["status"]: int(r["n"]) for r in rows}
        return {
            "pending": d.get("pending", 0),
            "approved": d.get("approved", 0),
            "rejected": d.get("rejected", 0),
        }
    except Exception:                            # noqa: BLE001
        return {"pending": 0, "approved": 0, "rejected": 0}


def state(conn) -> dict:
    """面板上要显示的全部东西，一次拿全，别让前端发一堆请求。"""
    n_msg = conn.execute("SELECT count(*) c FROM messages").fetchone()["c"]
    n_meme = conn.execute("SELECT count(*) c FROM memes").fetchone()["c"]
    n_vec = conn.execute("SELECT count(*) c FROM vectors").fetchone()["c"]
    n_fts = conn.execute("SELECT count(*) c FROM messages_fts").fetchone()["c"]

    def fsize(p: Path) -> str:
        try:
            return f"{p.stat().st_size / 1024 / 1024:.1f}MB"
        except OSError:
            return "-"

    meta = {}
    try:
        for r in conn.execute("SELECT k, v FROM meta").fetchall():
            meta[r["k"]] = r["v"]
    except Exception:                            # noqa: BLE001
        pass

    tasks = []
    for name, t in TASKS.items():
        j = JOBS.get(name, {})
        tasks.append({
            "name": name,
            "label": t["label"],
            "cost": t["cost"],
            "note": t["note"],
            "status": j.get("status", "idle"),
            "rc": j.get("rc"),
            "started": j.get("started"),
            "finished": j.get("finished"),
            "out": j.get("out") or "",
        })

    return {
        "counts": {
            "messages": n_msg,
            "memes": n_meme,
            "vectors": n_vec,
            "messages_fts": n_fts,
            "fts_ok": n_fts == n_msg,
            "memory": _memory_stats(conn),
            "facts": _facts_stats(conn),
        },
        "files": {
            "db": fsize(DB_PATH),
            "wal": fsize(Path(str(DB_PATH) + "-wal")),
            "packs": str(MEME_PACKS_DIR),
        },
        "raw_available": raw_available(),
        "meta": meta,
        "busy": _LOCK.locked(),
        "tasks": tasks,
    }
