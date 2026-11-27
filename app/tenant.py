"""A7 平台地基：租户 / API Key / 用量 / 任务编排。

为什么单独一个模块，而不是塞进 `main.py`：

1. 这三张表同时被三条线用（开放 API、运营中台、训练台），放业务代码里会互相打架；
2. API Key 的校验必须是**常量时间 + 只存哈希**，这种东西写散了早晚出事；
3. 任务编排以后要能被中台页面读，逻辑得在一个地方。

设计上的三个取舍（写在这里，免得以后忘）：

- **Key 只存 sha256**，明文只在创建时返回一次。数据库泄露不等于 Key 泄露。
- **配额按「天」算，UTC+8 跨日**。和 `whale-deep` 那套口径保持一致，别一个系统两种日期。
- **用量只记不删**（先用着），后面真的大了再加冷表；现在的量级一天撑死几千行。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT 'free',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  prefix TEXT NOT NULL,
  hash TEXT NOT NULL,
  quota_day INTEGER NOT NULL DEFAULT 200,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant_id);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  key_id TEXT,
  route TEXT NOT NULL,
  status INTEGER NOT NULL,
  ms INTEGER NOT NULL,
  tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  cron TEXT,
  script TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  timeout INTEGER NOT NULL DEFAULT 600,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  started TEXT NOT NULL,
  ended TEXT,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  exit_code INTEGER,
  log_path TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id, started);
"""

FREE_QUOTA = 200


def _now() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d")


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- 租户 / Key

def create_tenant(conn: sqlite3.Connection, name: str, plan: str = "free") -> dict:
    tid = "t_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO tenants (id, name, plan, created_at) VALUES (?,?,?,?)",
        (tid, name.strip() or "未命名", plan, _now()),
    )
    conn.commit()
    return {"id": tid, "name": name, "plan": plan}


def create_key(conn: sqlite3.Connection, tenant_id: str, quota_day: int = FREE_QUOTA) -> dict:
    """返回**唯一一次**能看到明文 Key 的结果，之后库里只有哈希。"""
    raw = "sk_live_" + secrets.token_urlsafe(24)
    kid = "k_" + uuid.uuid4().hex[:10]
    prefix = raw[:16]
    conn.execute(
        "INSERT INTO api_keys (id, tenant_id, prefix, hash, quota_day, created_at) VALUES (?,?,?,?,?,?)",
        (kid, tenant_id, prefix, hashlib.sha256(raw.encode()).hexdigest(), int(quota_day), _now()),
    )
    conn.commit()
    return {"id": kid, "key": raw, "prefix": prefix, "quota_day": int(quota_day)}


def lookup_key(conn: sqlite3.Connection, raw: str) -> dict | None:
    """按明文 Key 找记录。用 sha256 全表比一次也能接受（Key 数量是几十级）。"""
    if not raw or not raw.startswith("sk_live_"):
        return None
    h = hashlib.sha256(raw.encode()).hexdigest()
    row = conn.execute(
        "SELECT id, tenant_id, prefix, hash, quota_day, revoked FROM api_keys WHERE hash = ?",
        (h,),
    ).fetchone()
    if not row:
        return None
    # 常量时间比较：库里存的是哈希，这里再比一次是为了让「前缀对但哈希错」不产生时间差
    if not hmac.compare_digest(row["hash"], h) or row["revoked"]:
        return None
    return {"id": row["id"], "tenant_id": row["tenant_id"], "quota_day": int(row["quota_day"])}


def revoke_key(conn: sqlite3.Connection, key_id: str) -> int:
    cur = conn.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- 用量 / 配额

def used_today(conn: sqlite3.Connection, tenant_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM usage WHERE tenant_id = ? AND ts >= ?",
        (tenant_id, today() + " 00:00:00"),
    ).fetchone()
    return int(row[0] if row else 0)


def record(conn: sqlite3.Connection, tenant_id: str, key_id: str | None, route: str,
           status: int, ms: int, tokens: int = 0) -> None:
    conn.execute(
        "INSERT INTO usage (ts, tenant_id, key_id, route, status, ms, tokens) VALUES (?,?,?,?,?,?,?)",
        (_now(), tenant_id, key_id, route, int(status), int(ms), int(tokens)),
    )
    conn.commit()


def quota_left(conn: sqlite3.Connection, tenant_id: str, quota_day: int) -> int:
    return max(0, int(quota_day) - used_today(conn, tenant_id))


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*), SUM(ms), SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) FROM usage WHERE ts >= ?",
        (today() + " 00:00:00",),
    ).fetchone()
    n, total_ms, bad = (row[0] or 0, row[1] or 0, row[2] or 0) if row else (0, 0, 0)
    t = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()
    k = conn.execute("SELECT COUNT(*) FROM api_keys WHERE revoked = 0").fetchone()
    return {
        "tenants": int(t[0]) if t else 0,
        "keys": int(k[0]) if k else 0,
        "calls_today": int(n),
        "avg_ms": int(total_ms / n) if n else 0,
        "errors_today": int(bad),
    }


# ---------------------------------------------------------------- 任务编排

def ensure_job(conn: sqlite3.Connection, name: str, script: str, cron: str = "",
               timeout: int = 600) -> str:
    """按名字幂等注册一个任务（cron 只是展示用，真正的定时靠系统 crontab 或 ticker）。"""
    row = conn.execute("SELECT id FROM jobs WHERE name = ?", (name,)).fetchone()
    if row:
        conn.execute("UPDATE jobs SET script = ?, cron = ?, timeout = ? WHERE id = ?",
                     (script, cron, int(timeout), row["id"]))
        conn.commit()
        return row["id"]
    jid = "j_" + uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO jobs (id, name, cron, script, enabled, timeout, created_at) VALUES (?,?,?,?,1,?,?)",
        (jid, name, cron, script, int(timeout), _now()),
    )
    conn.commit()
    return jid


def start_run(conn: sqlite3.Connection, job_id: str) -> str:
    rid = "r_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO runs (id, job_id, started, status) VALUES (?,?,?, 'running')",
        (rid, job_id, _now()),
    )
    conn.commit()
    return rid


def finish_run(conn: sqlite3.Connection, run_id: str, status: str, exit_code: int | None = None,
               note: str = "", log_path: str = "") -> None:
    conn.execute(
        "UPDATE runs SET ended = ?, status = ?, exit_code = ?, note = ?, log_path = ? WHERE id = ?",
        (_now(), status, exit_code, note[:500], log_path, run_id),
    )
    conn.commit()


def recent_runs(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        """SELECT r.id, r.job_id, j.name, r.started, r.ended, r.status, r.exit_code
             FROM runs r LEFT JOIN jobs j ON j.id = r.job_id
            ORDER BY r.started DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]
