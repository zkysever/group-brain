"""A7 平台对外接口：/api/v1/*（Key 鉴权）+ /metrics（Prometheus 文本）。

三条线共用的「壳」都在这里，业务逻辑一律调现有模块（search / agent / lexicon），
这样开放 API 和网页端看到的结果永远一致——两份实现迟早对不上，这是踩过的坑。

鉴权口径（写清楚，免得以后自己都记不住）：

- `Authorization: Bearer sk_live_xxx` 或 `X-API-Key: sk_live_xxx`
- 没有 Key → 401；Key 错/被吊销 → 401；当天超额 → 429（带 `X-Quota-Left: 0`）
- 每次调用都记一条 `usage`，`/metrics` 就是把它聚合成 Prometheus 文本
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from . import agent, db, lexicon, search, tenant

router = APIRouter()


def _key_from(authorization: str | None, x_api_key: str | None) -> str:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _auth(conn: sqlite3.Connection, authorization: str | None, x_api_key: str | None) -> dict:
    raw = _key_from(authorization, x_api_key)
    if not raw:
        raise HTTPException(401, "缺少 API Key：Authorization: Bearer sk_live_…")
    hit = tenant.lookup_key(conn, raw)
    if not hit:
        raise HTTPException(401, "API Key 无效")
    left = tenant.quota_left(conn, hit["tenant_id"], hit["quota_day"])
    if left <= 0:
        raise HTTPException(429, "今日配额已用完")
    hit["left"] = left
    return hit


def _conn() -> sqlite3.Connection:
    return db.connect()


@router.get("/v1/ping")
def ping(authorization: str | None = Header(None), x_api_key: str | None = Header(None)) -> dict:
    conn = _conn()
    key = _auth(conn, authorization, x_api_key)
    tenant.record(conn, key["tenant_id"], key["id"], "/api/v1/ping", 200, 0)
    return {"ok": True, "tenant": key["tenant_id"], "quota_left": key["left"] - 1}


@router.get("/v1/search")
def v1_search(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(10, ge=1, le=30),
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
) -> dict:
    conn = _conn()
    key = _auth(conn, authorization, x_api_key)
    t0 = time.time()
    res = search.search_all(conn, q, limit=limit)
    ms = int((time.time() - t0) * 1000)
    tenant.record(conn, key["tenant_id"], key["id"], "/api/v1/search", 200, ms)
    return {
        "query": q,
        "mode": res.get("mode"),
        "took_ms": ms,
        "hits": [
            {"kind": h.get("kind"), "text": h.get("text"), "ts": h.get("ts"),
             "who": h.get("sender_name") or h.get("owner_name")}
            for h in (res.get("hits") or [])
        ],
    }


@router.get("/v1/lexicon")
def v1_lexicon(
    text: str = Query(..., min_length=1, max_length=200),
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
) -> dict:
    conn = _conn()
    key = _auth(conn, authorization, x_api_key)
    t0 = time.time()
    items = lexicon.glosses_for(conn, text)
    ms = int((time.time() - t0) * 1000)
    tenant.record(conn, key["tenant_id"], key["id"], "/api/v1/lexicon", 200, ms)
    return {"text": text, "took_ms": ms, "terms": items}


@router.post("/v1/ask")
async def v1_ask(request: Request, authorization: str | None = Header(None),
                 x_api_key: str | None = Header(None)) -> dict:
    """一次问答：走和网页端完全相同的 agent 流程（含工具调用），只是不带会话记忆。"""
    conn = _conn()
    key = _auth(conn, authorization, x_api_key)
    body = await request.json()
    q = str(body.get("q") or "").strip()
    if not q:
        raise HTTPException(400, "缺少 q")
    t0 = time.time()
    res = agent.run(conn, q, rounds=int(body.get("rounds") or 0) or None)
    ms = int((time.time() - t0) * 1000)
    tenant.record(conn, key["tenant_id"], key["id"], "/api/v1/ask", 200, ms)
    return {"answer": res.get("answer"), "took_ms": ms,
            "tool_calls": res.get("tool_calls"), "rounds": res.get("rounds")}


# ---------------------------------------------------------------- /metrics

def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def metrics_text(conn: sqlite3.Connection) -> str:
    day = tenant.today() + " 00:00:00"
    lines: list[str] = []
    st = tenant.stats(conn)
    lines.append("# HELP gb_tenants_total 租户数")
    lines.append("# TYPE gb_tenants_total gauge")
    lines.append(f"gb_tenants_total {st['tenants']}")
    lines.append("# HELP gb_api_keys_active 未吊销的 Key 数")
    lines.append("# TYPE gb_api_keys_active gauge")
    lines.append(f"gb_api_keys_active {st['keys']}")
    lines.append("# HELP gb_calls_today 今日调用数")
    lines.append("# TYPE gb_calls_today gauge")
    lines.append(f"gb_calls_today {st['calls_today']}")

    rows = conn.execute(
        "SELECT route, COUNT(*), AVG(ms), SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END)"
        "  FROM usage WHERE ts >= ? GROUP BY route", (day,)
    ).fetchall()
    lines.append("# HELP gb_requests_total 按路由累计的请求数")
    lines.append("# TYPE gb_requests_total counter")
    for r in rows:
        lines.append(f'gb_requests_total{{route="{_esc(r[0])}"}} {r[1]}')
    lines.append("# HELP gb_request_ms_avg 按路由的平均耗时（毫秒）")
    lines.append("# TYPE gb_request_ms_avg gauge")
    for r in rows:
        lines.append(f'gb_request_ms_avg{{route="{_esc(r[0])}"}} {float(r[2] or 0):.1f}')
    lines.append("# HELP gb_errors_total 按路由的错误数")
    lines.append("# TYPE gb_errors_total counter")
    for r in rows:
        lines.append(f'gb_errors_total{{route="{_esc(r[0])}"}} {int(r[3] or 0)}')

    runs = conn.execute(
        "SELECT status, COUNT(*) FROM runs WHERE started >= ? GROUP BY status", (day,)
    ).fetchall()
    lines.append("# HELP gb_job_runs_today 今日任务运行数（按状态）")
    lines.append("# TYPE gb_job_runs_today gauge")
    for r in runs:
        lines.append(f'gb_job_runs_today{{status="{_esc(r[0])}"}} {r[1]}')
    if not runs:
        lines.append('gb_job_runs_today{status="none"} 0')

    lines.append("# HELP gb_health 1=库可读")
    lines.append("# TYPE gb_health gauge")
    try:
        conn.execute("SELECT 1").fetchone()
        lines.append("gb_health 1")
    except Exception:                                  # noqa: BLE001
        lines.append("gb_health 0")
    return "\n".join(lines) + "\n"


metrics_router = APIRouter()


@metrics_router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus 抓这个。**故意不鉴权**：它只暴露聚合数字，不暴露内容；
    要挡就在反代那层挡来源 IP。"""
    return metrics_text(db.connect())


def install(app) -> None:
    app.include_router(router, prefix="/api")
    app.include_router(metrics_router)
