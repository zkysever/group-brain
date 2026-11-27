"""A7 任务 ticker：每 60 秒扫一次 `jobs`，到点就跑，跑完写 `runs`。

为什么先写 ticker 再迁 cron：

现在 12 条 cron 各跑各的，挂了没人知道（上周 `sync_messages` 停了一周才发现库里
数据停在 11-05）。ticker 的作用不是「替换 crontab」，是**给每次运行留下一条记录**：
谁跑的、多久、成不成、日志在哪。等它稳了，再把 crontab 一条条摘掉。

三个刻意的设计：

1. **单进程单线程**：2C1.7G 的机器上并发跑两个渲染会 OOM，宁可排队。
2. **不完全信任 `cron` 字段**：这一版只做「手动触发」和「固定间隔」，真正的 cron 解析
   留到 crontab 迁入时再做（自己写 cron 解析器是自找麻烦）。
3. **锁文件**：ticker 只允许一个实例（systemd 里 `Restart=always` 时容易起两个）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import db, tenant

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "data" / "ticker.lock"
TICK = int(os.environ.get("TICKER_SECONDS", "60"))
MAX_MINUTES = int(os.environ.get("TICKER_MAX_MINUTES", "30"))


def _log(msg: str) -> None:
    print(f"[ticker] {msg}", flush=True)


def _acquire() -> bool:
    """单实例：拿不到锁就退出（第二个实例不该抢任务）。"""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # 老锁可能是上次被 kill 留下的：超过 2 小时就当僵尸删掉
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            age = 0
        if age > 7200:
            LOCK.unlink(missing_ok=True)
            return _acquire()
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def release() -> None:
    LOCK.unlink(missing_ok=True)


def run_job(conn, job: dict) -> dict:
    """跑一个任务：起 run 记录 → 子进程 → 写结果。超时就杀。"""
    rid = tenant.start_run(conn, job["id"])
    script = ROOT / job["script"]
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{job['id']}-{rid}.log"
    _log(f"run {job['name']} ({job['script']}) → {log_path.name}")
    t0 = time.time()
    code = -1
    note = ""
    try:
        with log_path.open("wb") as fh:
            proc = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                timeout=int(job["timeout"] or 600),
            )
        code = proc.returncode
    except subprocess.TimeoutExpired:
        note = f"超时（>{job['timeout']}s）"
        code = -9
    except Exception as exc:                     # noqa: BLE001
        note = f"{type(exc).__name__}: {exc}"
    status = "ok" if code == 0 else "fail"
    tenant.finish_run(conn, rid, status, code, note,
                      str(log_path.relative_to(ROOT)))
    _log(f"done {job['name']} status={status} code={code} {int(time.time()-t0)}s")
    return {"run": rid, "status": status, "exit_code": code, "seconds": int(time.time() - t0)}


def due_jobs(conn, every_minutes: int) -> list[dict]:
    """挑「该跑的」：从未跑过，或者上次跑完已经超过 every_minutes。"""
    rows = conn.execute(
        "SELECT j.*, (SELECT MAX(started) FROM runs r WHERE r.job_id = j.id) AS last"
        "  FROM jobs j WHERE j.enabled = 1"
    ).fetchall()
    now = time.time()
    out = []
    for r in rows:
        last = r["last"]
        if not last:
            out.append(dict(r))
            continue
        if every_minutes <= 0:
            # 0 的意思是「只跑从没跑过的」；写成 `>= 0` 会让每一轮都重跑全部任务，
            # 自检时就这么错过一次（tick 完立刻又全部 due）。
            continue
        try:
            ts = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            out.append(dict(r))
            continue
        if (now - ts) / 60.0 >= every_minutes:
            out.append(dict(r))
    return out


def tick(conn, every_minutes: int = 0) -> list[dict]:
    """跑一轮。every_minutes=0 时只跑「从没跑过」的任务。"""
    did = []
    for job in due_jobs(conn, every_minutes):
        did.append(run_job(conn, job))
        if did and did[-1]["exit_code"] == -9:
            break                                # 超时就别再往下排了，机器已经吃紧
    return did


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if arg == "once":
        conn = db.connect()
        tenant.ensure(conn)
        print(json.dumps(tick(conn, 0), ensure_ascii=False))
        return 0
    if not _acquire():
        _log("已经有一个 ticker 在跑，退出")
        return 0
    _log(f"启动，每 {TICK}s 一轮，单任务上限 {MAX_MINUTES} 分钟")
    try:
        while True:
            try:
                conn = db.connect()
                tenant.ensure(conn)
                tick(conn, MAX_MINUTES)
            except Exception as exc:             # noqa: BLE001
                _log(f"这一轮挂了：{type(exc).__name__}: {exc}")
            time.sleep(TICK)
    finally:
        release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
