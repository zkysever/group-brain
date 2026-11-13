"""FastAPI 入口：网页 + 两个 JSON 接口 + 一道口令门。

启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

口令门：除了 /login、/api/health，所有路径都要带合法 cookie，否则
  - 页面 → 302 到 /login
  - /api/* → 401 JSON（前端收到就跳 /login）
"""
from __future__ import annotations

import mimetypes
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import admin, agent, auth, coach, db, facts, learn, memory, search
from . import config as cfg
from .config import MEME_PACKS_DIR, WEB_DIR

app = FastAPI(title="group-brain", version="0.16.0")

# 不需要口令就能碰的路径。健康检查放开，方便外部探活 / 手机调试。
PUBLIC = {"/login", "/api/health", "/favicon.ico", "/portfolio"}

_ERR_TEXT = {
    "bad": "口令不对，再试一次。",
    "too_many": "连错太多次了，等十分钟再来。",
}

# 别指望系统自带的 mime 表：Ubuntu 上没装 media-types 时 guess_type 认不出 .png，
# 会把图片发成 application/octet-stream。这里自己兜一张小表。
_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_local = threading.local()


def conn():
    """每个线程一条连接。

    这里踩过一次坑：原来用一个进程级全局连接，本地单线程手测一切正常，
    一上线就集体 500 —— FastAPI 的同步接口跑在线程池里，同一条 sqlite
    连接被不同线程拿去用，直接抛
    "SQLite objects created in a thread can only be used in that same thread"。
    改成 thread-local 之后每个工作线程各用各的，也就不需要连接池了。
    """
    c = getattr(_local, "conn", None)
    if c is None:
        c = db.connect()
        _local.conn = c
    return c


@app.middleware("http")
async def gate(request: Request, call_next):
    """所有请求先过这道门；放行的两种：公开路径、cookie 合法。"""
    path = request.url.path
    if path in PUBLIC or auth.is_open(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "需要口令", "login": "/login"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(err: str = ""):
    page = WEB_DIR / "login.html"
    if not page.exists():
        raise HTTPException(500, "web/login.html 不见了")
    msg = _ERR_TEXT.get(err, "")
    block = f'<p class="err">{msg}</p>' if msg else ""
    return page.read_text(encoding="utf-8").replace("{{ERR}}", block)


@app.post("/login")
async def login_submit(request: Request):
    """表单解析用手写：只为这一处装 python-multipart 不值。"""
    ip = request.client.host if request.client else "unknown"
    if auth.too_many(ip):
        return RedirectResponse("/login?err=too_many", status_code=303)

    raw = (await request.body()).decode("utf-8", "replace")
    pw = (parse_qs(raw).get("pw") or [""])[0]

    if not auth.check_password(pw):
        auth.record_fail(ip)
        return RedirectResponse("/login?err=bad", status_code=303)

    auth.clear_fails(ip)
    resp = RedirectResponse("/", status_code=303)
    # Cloudflare 会把协议改写，真实协议看 X-Forwarded-Proto
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    resp.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_token(),
        max_age=auth.MAX_AGE,
        httponly=True,
        secure=(scheme == "https"),
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(500, "web/index.html 不见了")
    return page.read_text(encoding="utf-8")


@app.get("/api/search")
def api_search(
    q: str = Query("", description="搜索词"),
    kind: str = Query("all", description="all / message / meme"),
    limit: int = Query(10, ge=1, le=50),
    mode: str = Query("auto", description="auto / keyword / vector / hybrid"),
):
    q = q.strip()
    if not q:
        return {"query": "", "kind": kind, "mode": mode, "took_ms": 0, "count": 0, "hits": []}
    return search.search_all(conn(), q, kind, limit, mode)


@app.get("/api/stats")
def api_stats():
    return search.stats(conn())


@app.get("/api/context")
def api_context(
    id: int = Query(..., description="消息 id（搜索结果里的那条）"),
    before: int = Query(3, ge=0, le=10, description="往前几条"),
    after: int = Query(3, ge=0, le=10, description="往后几条"),
):
    """v0.2 记忆回放：给一条消息，把前后的对话一起端出来。

    为什么值得做：搜索给的是「一句孤立的话」。但群里的话有一半意思在上下文里——
    「你试试」单看莫名其妙，配上前面那句「我吃了三碗」就笑了。
    """
    rows = db.window(conn(), id, before, after)
    if not rows:
        raise HTTPException(404, "没有这条消息")
    return {
        "target_id": id,
        "count": len(rows),
        "span": [rows[0]["ts"], rows[-1]["ts"]],
        "messages": [
            {
                "id": r["id"],
                "ts": r["ts"],
                "sender_name": r["sender_name"],
                "kind": r["kind"],
                "text": r["text"],
                "target": r["id"] == id,
            }
            for r in rows
        ],
    }


@app.get("/agent", response_class=HTMLResponse)
def agent_page() -> str:
    page = WEB_DIR / "agent.html"
    if not page.exists():
        raise HTTPException(500, "web/agent.html 不见了")
    return page.read_text(encoding="utf-8")


def _agent_session(request: Request) -> tuple[str, bool]:
    """返回 (会话 id, 是不是新开的)。

    v0.16：默认把记忆挂到共享会话 `main` 上——口令本身就是身份，同一个口令
    在手机上问过的，电脑上接着问也记得。`AGENT_SHARED_MEMORY=0` 可以退回
    「每台浏览器一个会话」的老行为。

    升级时那段记忆不能丢：共享会话还空着的话，把最近用过的那个旧 cookie
    会话整段搬过来（`memory.adopt_into`），否则用户第一句话就会觉得「它忘了」。
    """
    if cfg.AGENT_SHARED_MEMORY:
        n = memory.adopt_into(conn())
        if n:
            print(f"[mem] 把旧会话的 {n} 条记忆并入共享会话")
        return memory.SHARED_SESSION, False

    session = (request.cookies.get("gb_sess") or "").strip()
    if session:
        return session, False
    return memory.new_session(), True


@app.get("/api/agent")
def api_agent(
    request: Request,
    q: str = Query("", description="用户的一句话"),
    rounds: int = Query(0, ge=0, le=6, description="最多允许几轮工具调用，0 = 用配置默认值"),
    history: str = Query("", description='（兼容旧客户端）刚才的对话，JSON： [{"role":"user","content":"…"}]'),
):
    """A2：把检索当工具交给模型，由它自己决定调不调、调哪个。

    记忆现在存在服务端（2026-11-12 改的）：浏览器带一个 `gb_sess` cookie，
    每轮问答写进 `agent_turns` 表，下一次提问从库里取最近 N 轮。
    好处是刷新、换设备都还在；坏处是得多一次读写库——所以顺手加了 `/api/agent/forget`。

    `history` 参数还留着，但只是给老客户端兜底；服务端有记忆时以服务端为准。
    """
    q = q.strip()
    if not q:
        return {"ok": False, "error": "问题不能为空", "answer": "", "trace": []}

    c = conn()
    session, fresh = _agent_session(request)

    past = memory.recent(c, session, agent.HISTORY_TURNS)
    if not past and history:
        # 旧的浏览器内存方案：只有在服务端确实没记录时才用它，避免两套上下文打架
        past = agent._norm_history(history)

    res = agent.run(c, q, max_rounds=rounds or agent.MAX_ROUNDS, history=past)

    # 只记问答，不记工具轨迹：工具结果每条几百字，带进下一轮纯属浪费
    if res.get("ok"):
        memory.append(c, session, "user", q)
        memory.append(c, session, "assistant", res.get("answer", ""))
        memory.trim(c, session, memory.KEEP_TURNS)
    res["memory_turns"] = memory.turn_count(c, session)
    res["session_new"] = fresh
    res["memory_shared"] = cfg.AGENT_SHARED_MEMORY

    resp = JSONResponse(res)
    if fresh and not cfg.AGENT_SHARED_MEMORY:
        resp.set_cookie(
            "gb_sess",
            session,
            max_age=400 * 24 * 3600,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return resp


@app.api_route("/api/agent/forget", methods=["GET", "POST"])
def api_agent_forget(request: Request):
    """清空当前记忆。用户要求「别再删记忆」，但总得留个能删的按钮。

    v0.16 起记忆是共享的：手机上点一下，电脑上那段也一起清掉——所以返回里
    带 `shared`，前端提示语也要说清楚。
    """
    session, _ = _agent_session(request)
    return {
        "ok": True,
        "deleted": memory.forget(conn(), session),
        "shared": cfg.AGENT_SHARED_MEMORY,
    }


@app.post("/api/agent/fix")
def api_agent_fix(
    request: Request,
    q: str = Query(..., description="用户当时问的那句话"),
    right: str = Query(..., description="正确答案"),
    wrong: str = Query("", description="它当时答错的（可选，留着方便回头看）"),
):
    """教它一句（v0.13 纠错记忆）。

    参数走 query string 而不是表单：服务器没装 python-multipart，
    `Form(...)` 会让启动时直接报错，为一条纠正去加依赖不值得。
    """
    session, _ = _agent_session(request)
    try:
        cid = learn.record(conn(), q, right, wrong=wrong, source="web", session_id=session)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "id": cid, "total": learn.stats(conn())["total"]}


@app.get("/api/agent/fixes")
def api_agent_fixes(limit: int = Query(50, ge=1, le=200)):
    """它现在「学会」了哪些（按时间倒序）。学错了能删，否则错误会被一直放大。"""
    c = conn()
    return {"ok": True, **learn.stats(c), "rows": learn.all_rows(c, limit)}


@app.post("/api/agent/fix/delete")
def api_agent_fix_delete(id: int = Query(..., description="corrections 表的 id")):
    return {"ok": True, "deleted": learn.delete(conn(), id)}


@app.get("/facts", response_class=HTMLResponse)
def facts_page() -> str:
    page = WEB_DIR / "facts.html"
    if not page.exists():
        raise HTTPException(500, "web/facts.html 不见了")
    return page.read_text(encoding="utf-8")


@app.get("/api/facts")
def api_facts(
    status: str = Query("pending", description="pending / approved / rejected / all"),
    limit: int = Query(100, ge=1, le=500),
):
    """待审的事实草稿（v0.14）。默认只看 pending —— 审阅页的唯一任务是清空它。"""
    c = conn()
    rows = facts.list_rows(c, status=status, limit=limit)
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "ts": r["ts"],
                "subject": r["subject"],
                "claim": r["claim"],
                "quote": r["quote"] or "",
                "msg_ids": [int(x) for x in (r["msg_ids"] or "").split(",") if x.strip().isdigit()],
                "status": r["status"],
                "hits": r["hits"],
            }
        )
    return {"ok": True, "status": status, "count": len(out), **facts.stats(c), "rows": out}


@app.post("/api/facts/decide")
def api_facts_decide(
    id: int = Query(..., description="facts 表的 id"),
    status: str = Query(..., description="approved / rejected / pending"),
):
    """点头或摇头。点头之后它会立刻进检索（FTS 重建 + 下一次回答就能引用）。"""
    if status not in ("approved", "rejected", "pending"):
        return {"ok": False, "error": "status 只能是 approved / rejected / pending"}
    c = conn()
    n = facts.decide(c, id, status)
    return {"ok": bool(n), "changed": n, **facts.stats(c)}


@app.post("/api/facts/delete")
def api_facts_delete(id: int = Query(..., description="facts 表的 id")):
    c = conn()
    return {"ok": True, "deleted": facts.delete(c, id), **facts.stats(c)}


@app.get("/api/rules")
def api_rules():
    """自学规则（v0.15）：从评测集挖出来、通过留出集影子测试的那几条。

    默认全部 enabled=0。这里只负责列出和开关，不负责挖——
    挖规则要跑评测集，属于离线动作，放在 scripts/mine_rules.py 和 /admin 里。
    """
    c = conn()
    rows = []
    try:
        for r in c.execute(
            "SELECT id, pattern, tool, evidence, precision, note, enabled, ts"
            " FROM rules ORDER BY enabled DESC, evidence DESC, length(pattern) DESC"
        ):
            rows.append(dict(r))
    except Exception:                            # noqa: BLE001
        rows = []
    return {"ok": True, "count": len(rows), "enabled": sum(1 for r in rows if r["enabled"]), "rows": rows}


@app.post("/api/rules/toggle")
def api_rules_toggle(
    id: int = Query(..., description="rules 表的 id"),
    enabled: int = Query(..., ge=0, le=1),
):
    """点头/取消。点头之后它参与「前置预查」——错了会立刻体现在下一次回答里。"""
    c = conn()
    try:
        cur = c.execute("UPDATE rules SET enabled = ? WHERE id = ?", (int(enabled), int(id)))
        c.commit()
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": bool(cur.rowcount), "changed": int(cur.rowcount or 0)}


@app.post("/api/rules/delete")
def api_rules_delete(id: int = Query(..., description="rules 表的 id")):
    c = conn()
    cur = c.execute("DELETE FROM rules WHERE id = ?", (int(id),))
    c.commit()
    return {"ok": True, "deleted": int(cur.rowcount or 0)}


@app.get("/coach", response_class=HTMLResponse)
def coach_page() -> str:
    page = WEB_DIR / "coach.html"
    if not page.exists():
        raise HTTPException(500, "web/coach.html 不见了")
    return page.read_text(encoding="utf-8")


@app.get("/api/coach")
def api_coach_get(week: str = Query("", description="ISO 周，如 2026-W46；留空=本周")):
    """读：某一周的周报 + 最近生成过哪些周。"""
    c = conn()
    wk = week.strip() or coach.iso_week()
    try:
        rep = coach.get(c, wk)
    except Exception as e:
        return {"ok": False, "error": str(e), "reports": []}
    return {"ok": True, "week": wk, "report": rep, "reports": coach.recent(c)}


@app.post("/api/coach")
def api_coach_gen(week: str = Query("", description="ISO 周；留空=本周")):
    """写：生成（或重算）某一周的周报。要调一次 DeepSeek，约 10~20 秒。

    单独做成 POST 是因为它会花钱、也慢；GET 让它顺带生成的话，
    刷新页面就会重复扣钱，这不是一个页面该有的行为。
    """
    try:
        res = coach.generate(conn(), week, save=True)
    except Exception as e:                      # noqa: BLE001 —— 生成失败要把原因原样告诉用户
        return {"ok": False, "error": str(e)}
    res["facts_text"] = coach.render_facts(res["facts"])
    return res


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page() -> str:
    """作品集页：不需要口令。

    理由是这一页存在的目的就是「发给别人看」——面试官、群友、视频观众。
    要口令的页面等于没贴出去。页面上只有聚合数字和结论，没有任何一条原始发言。
    """
    page = WEB_DIR / "portfolio.html"
    if not page.exists():
        raise HTTPException(500, "web/portfolio.html 不见了")
    st = search.stats(conn())
    html = page.read_text(encoding="utf-8")
    # 数字取实时值：搬一次服务器、重导一次数据，这页自己会跟上，不用手改
    for key, val in (
        ("{{MSG}}", st["messages"]),
        ("{{MEME}}", st["memes"]),
        ("{{VEC}}", st["vectors"]),
        ("{{COMMITS}}", _commit_count()),
    ):
        html = html.replace(key, str(val))
    return html


# 提交数不能写死：写在页面上那一刻就开始过期，而这是要给人看的东西。
# 用 GitHub API 拿一次（读 Link 头的 last 页号 = 总条数），缓存 10 分钟，
# 拿不到就显示「—」——宁可空着，也不显示一个假数字。
_COMMITS: dict[str, object] = {"n": None, "at": 0.0}


def _commit_count() -> str:
    now = time.time()
    if _COMMITS["n"] and now - float(_COMMITS["at"]) < 600:
        return str(_COMMITS["n"])
    tok = coach._gh_token()
    if not tok:
        return "—"
    try:
        r = requests.get(
            f"https://api.github.com/repos/{coach.REPO}/commits",
            params={"per_page": 1},
            headers={"Authorization": f"Bearer {tok}", "User-Agent": "group-brain"},
            timeout=5,
        )
        m = re.search(r'[?&]page=(\d+)>; rel="last"', r.headers.get("Link", ""))
        n = m.group(1) if m else ("1" if r.status_code == 200 else "—")
    except Exception:
        n = "—"
    # 失败不进缓存：网络抖一下就把公开页上的数字空 10 分钟，不值当
    if n != "—":
        _COMMITS.update({"n": n, "at": now})
    return n


@app.get("/admin", response_class=HTMLResponse)
def admin_page() -> str:
    """v0.7 管理面板。薄薄一层：点按钮 = 在服务器上跑一次 scripts/ 里的脚本。"""
    page = WEB_DIR / "admin.html"
    if not page.exists():
        raise HTTPException(500, "web/admin.html 不见了")
    return page.read_text(encoding="utf-8")


@app.get("/api/admin/status")
def api_admin_status():
    return admin.state(conn())


@app.post("/api/admin/run")
def api_admin_run(task: str = Query(..., description="任务名，见 /api/admin/status 的 tasks")):
    return admin.start(task)


@app.get("/api/meme/{meme_id}")
def api_meme(meme_id: int):
    row = conn().execute("SELECT path FROM memes WHERE id = ?", (meme_id,)).fetchone()
    if not row:
        raise HTTPException(404, "没有这张图")
    p = Path(row["path"])
    if not p.is_absolute():          # 库里存的是相对 MEME_PACKS_DIR 的路径
        p = MEME_PACKS_DIR / p
    if not p.exists():
        raise HTTPException(404, f"图片文件不在: {p}")
    media = _MEDIA.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0]
    return FileResponse(p, media_type=media or "application/octet-stream")


@app.get("/api/health")
def api_health():
    """探活要看两件事：进程活着，库能读。

    早期只回 {"ok": true}，结果库炸了它还报健康，白瞎——自己骗自己。
    现在把库能不能读、有多少条数据一起报出来。
    """
    try:
        s = search.stats(conn())
        return {
            "ok": True,
            "version": app.version,
            "db_ok": True,
            "messages": s["messages"],
            "memes": s["memes"],
            "vectors": s["vectors"],
            "vector_ok": s["vector_ok"],
        }
    except Exception as e:  # 库坏了也要能探活，否则监控永远只看到 502
        return JSONResponse(
            {"ok": True, "version": app.version, "db_ok": False,
             "error": f"{type(e).__name__}: {e}"},
            status_code=200,
        )
