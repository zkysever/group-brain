"""FastAPI 入口：网页 + 两个 JSON 接口 + 一道口令门。

启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

口令门：除了 /login、/api/health，所有路径都要带合法 cookie，否则
  - 页面 → 302 到 /login
  - /api/* → 401 JSON（前端收到就跳 /login）
"""
from __future__ import annotations

import hmac
import mimetypes
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import admin, agent, auth, bili, bili_writer, coach, curator, db, facts, learn, lexicon
from . import memory, search, series
from . import config as cfg
from . import vectors
from .config import MEME_PACKS_DIR, WEB_DIR

app = FastAPI(title="group-brain", version="0.22.2")


@app.on_event("startup")
def _startup() -> None:
    """启动时跑一次建表 + 迁移。

    以前 `db.init()` 只在 scripts/init_db.py 里调，线上靠部署脚本记得跑；
    忘了跑、或者跑的时候库被锁住，表现是「草稿永远存不上」这种很晚才发现的怪现象。
    现在启动就补，失败就直接起不来（见 db._migrate 的说明）。
    """
    vectors.clear_cache()   # 进程刚起来，别继承任何旧矩阵
    db.init()

# 不需要口令就能碰的路径。健康检查放开，方便外部探活 / 手机调试。
# /api/lexicon/* 放开是给 AstrBot 插件用的：它在 docker 里，带不上口令 cookie，
# 改用 header 里的 `X-GB-Token`（见 _lex_ok），不校验就一律 401。
PUBLIC = {
    "/login", "/api/health", "/favicon.ico", "/portfolio", "/series", "/api/series",
    "/api/lexicon/context", "/api/lexicon/observe", "/api/lexicon/correct",
    "/api/lexicon/confirm",
}

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
    # /out/ 让投稿页跨域取视频用（跨域请求带不上 cookie），它自己按 OUT_TOKEN 校验。
    # 但只有「真配了 OUT_TOKEN」才放行：否则 /out/ 就是一个匿名文件服务器，
    # 视频、封面连同未来往那里丢的任何东西都能被任何人下载。
    if (
        path in PUBLIC
        or (path.startswith("/out/") and cfg.OUT_TOKEN)
        or auth.is_open(request.cookies.get(auth.COOKIE_NAME))
    ):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "需要口令", "login": "/login"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


def client_ip(request: Request) -> str:
    """取真实来源 IP。

    线上挂在 Cloudflare 后面，`request.client.host` 永远是 CF 的地址：
    所有人共用一个登录失败桶 → 谁故意打错 10 次，全站登录被挡 10 分钟。
    取 X-Forwarded-For 最左一跳（CF 会追加，最左是真实客户端）。
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


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
    ip = client_ip(request)
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


_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _session_lock(session: str) -> threading.Lock:
    """按会话取一把锁（同一个会话串行，不同会话并发）。

    锁表本身很小（一个口令 = 一个会话），但 `AGENT_SHARED_MEMORY=0` 时每个浏览器
    一个会话，所以要有个上限：超过 500 个就把整张表清掉重建——清错最多丢一次
    串行性，不会丢数据，比无限长着占用内存好。
    """
    with _SESSION_LOCKS_GUARD:
        if len(_SESSION_LOCKS) > 500:
            _SESSION_LOCKS.clear()
        lock = _SESSION_LOCKS.get(session)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[session] = lock
        return lock


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


@app.post("/api/agent")
def api_agent(
    request: Request,
    q: str = Query("", description="用户的一句话"),
    rounds: int = Query(0, ge=0, le=6, description="最多允许几轮工具调用，0 = 用配置默认值"),
    history: str = Query("", description='（兼容旧客户端）刚才的对话，JSON： [{"role":"user","content":"…"}]'),
):
    """A2：把检索当工具交给模型，由它自己决定调不调、调哪个。

    2026-11-19 从 GET 改成 POST：GET 会写记忆、烧 DeepSeek 额度，
    而 cookie 是 samesite=lax —— 只挡跨站子资源，顶层导航照样带 cookie。
    任何页面放一个 `<a href="…/api/agent?q=…">`、或者聊天软件生成链接预览，
    就能替用户问一句并写进共享记忆。

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

    # 同一个会话串行跑：共享记忆下两个人同时问，会各自读到同一份「上文」，
    # 然后四条消息乱序落库，模型下一轮就会看到「答在问前面」。
    # 锁只按会话分，不同会话之间照旧并发。
    lock = _session_lock(session)
    with lock:
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


@app.post("/api/agent/forget")
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


# --------------------------------------------------------------- 群内自学（A6）
# 三个接口给 AstrBot 插件用：它在 docker 里，带不上口令 cookie，所以放行后用
# `X-GB-Token` 再校验一道（值来自 .env 的 LEARN_TOKEN，没配就是 OUT_TOKEN）。

def _lex_ok(request: Request) -> bool:
    want = cfg.LEARN_TOKEN
    if not want:
        return False
    got = request.headers.get("x-gb-token", "")
    # 常量时间比较：token 是写权限凭证，别给计时侧信道留口子。
    return bool(got) and hmac.compare_digest(got, want)


@app.get("/api/lexicon/context")
def api_lexicon_context(request: Request, text: str = Query("", max_length=500)):
    """回复插件在问模型之前调它：这句话里有哪些「已经学会的词」。

    只查库，不上网、不调模型 —— 群聊回复不能为了一个词等 5 秒。
    """
    if not _lex_ok(request):
        return JSONResponse({"detail": "token 不对"}, status_code=401)
    c = conn()
    hits = lexicon.glosses_for(c, text, limit=3)
    return {
        "ok": True,
        "glosses": [{"term": h["term"], "gloss": h["gloss"], "kind": h["kind"]} for h in hits],
    }


@app.post("/api/lexicon/observe")
async def api_lexicon_observe(request: Request):
    """回复完把这一阵群聊丢过来复盘：判词 → 上网查 → 记住。立刻返回，学习在后台。"""
    if not _lex_ok(request):
        return JSONResponse({"detail": "token 不对"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "body 不是 JSON"}
    lines = body.get("lines") or []
    if not isinstance(lines, list) or not lines:
        return {"ok": False, "error": "lines 为空"}
    clean = [
        {"who": str(x.get("who") or "")[:40], "text": str(x.get("text") or "")[:300]}
        for x in lines[:20]
        if isinstance(x, dict) and str(x.get("text") or "").strip()
    ]
    if not clean:
        return {"ok": False, "error": "没有有效文本"}
    return {"ok": True, **lexicon.observe_async(clean, str(body.get("reply") or "")[:300])}


@app.post("/api/lexicon/correct")
async def api_lexicon_correct(request: Request):
    """学错了怎么办：群里有人纠正时直接把解释覆盖掉。"""
    if not _lex_ok(request):
        return JSONResponse({"detail": "token 不对"}, status_code=401)
    body = await request.json()
    try:
        lexicon.correct(conn(), str(body.get("term") or ""), str(body.get("gloss") or ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


@app.post("/api/lexicon/confirm")
async def api_lexicon_confirm(request: Request):
    """人点头：把「只凭上下文猜的」词放行（可以顺手改一句解释）。"""
    if not _lex_ok(request):
        return JSONResponse({"detail": "token 不对"}, status_code=401)
    body = await request.json()
    try:
        n = lexicon.confirm(conn(), str(body.get("term") or ""), str(body.get("gloss") or ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "confirmed": n}


@app.get("/api/lexicon/list")
def api_lexicon_list(limit: int = Query(100, ge=1, le=400)):
    """它自学了哪些词（这个走口令，是给人看的）。"""
    c = conn()
    return {"ok": True, **lexicon.stats(c), "rows": lexicon.all_rows(c, limit),
            "worker": lexicon.state()}


@app.post("/api/lexicon/reject")
def api_lexicon_reject(term: str = Query(..., description="学错的那个词")):
    return {"ok": True, "rejected": lexicon.reject(conn(), term)}


@app.post("/api/lexicon/mine")
def api_lexicon_mine(limit: int = Query(8, ge=1, le=20), days: int = Query(2, ge=1, le=7)):
    """手动跑一次「挖词」：从最近的群聊里找群内说法（cron 每天也会跑）。"""
    return {"ok": True, **lexicon.mine(conn(), limit=limit, days=days)}


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


@app.get("/api/series")
def api_series() -> dict:
    """系列目录：哪些期已发布（有 BV 号）、哪些排了还没到点。"""
    return series.episodes()


@app.get("/series", response_class=HTMLResponse)
def series_page() -> str:
    """系列目录页：给视频简介里的链接用，不需要口令。

    合集功能要电磁力 Lv2（粉丝 ≥100），现在开不了，所以先用这一页当「站外合集」：
    每期视频简介底部都链过来，观众能顺着一集集看下去。
    """
    page = WEB_DIR / "series.html"
    if not page.exists():
        raise HTTPException(500, "web/series.html 不见了")
    return page.read_text(encoding="utf-8")


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


# 要发出去的东西（视频、封面）放 out/，手机要能直接下载 —— B 站 App 投稿只能选本机文件。
_OUT_DIR = Path(__file__).resolve().parent.parent / "out"
_OUT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@app.get("/out/{name}")
def out_file(name: str, k: str = Query("", description="OUT_TOKEN，投稿页 fetch 用")):
    if not _OUT_NAME.match(name):
        raise HTTPException(404, "没有这个文件")
    # 这一路是给「投稿页把视频读成 blob」用的：跨域 fetch 带不上口令 cookie，
    # 所以用 token 代替口令。**没配 token 就直接 404**——以前是「不配就不校验」，
    # 等于把 out/ 变成一个匿名文件服务器（口令门对 /out/ 也是放行的）。
    if not cfg.OUT_TOKEN:
        raise HTTPException(404, "没有这个文件")
    # 常量时间比较，别给 token 留计时侧信道
    if not hmac.compare_digest(k, cfg.OUT_TOKEN):
        raise HTTPException(404, "没有这个文件")
    root = _OUT_DIR.resolve()
    p = (root / name).resolve()
    # 只允许 root 下的直接子文件，挡 ../ 之类
    if p.parent != root or not p.is_file():
        raise HTTPException(404, "没有这个文件")
    # 带 CORS：投稿页（member.bilibili.com）要用 fetch 把这份视频读成 blob 再塞进
    # 它的 <input type=file>，不给跨域头浏览器会直接拦掉。
    return FileResponse(
        p,
        filename=name,
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
    )


@app.get("/lexicon", response_class=HTMLResponse)
def lexicon_page() -> str:
    """群内词库审阅页：猜出来的词在这里点头/驳回/改。

    为什么要有这一页：`confirm`/`reject` 之前只有 token 接口，插件够用、人没法用。
    而 A6 的规矩恰恰是「猜的词不许自己生效，要人点头」——没有页面，这条规矩等于没落地。
    """
    page = WEB_DIR / "lexicon.html"
    if not page.exists():
        raise HTTPException(500, "web/lexicon.html 不见了")
    return page.read_text(encoding="utf-8")


@app.get("/api/lexicon/rows")
def api_lexicon_rows(status: str = Query("", description="空=全部；guess/auto/confirmed/rejected")):
    """词库列表（走口令门，不需要 x-gb-token —— 这是给人看的）。"""
    c = conn()
    rows = lexicon.all_rows(c)
    out = []
    for r in rows:
        st = r["status"]
        if status and st != status:
            continue
        out.append(
            {
                "term": r["term"],
                "gloss": r["gloss"] or "",
                "kind": r["kind"] or "",
                "source": r["source"] or "",
                "confidence": round(float(r["confidence"] or 0), 2),
                "status": st,
                "hits": int(r["hits"] or 0),
                "asked": int(r["asked"] or 0),
                "updated": r["updated"] or "",
            }
        )
    # 待审的排前面：这一页的唯一任务是「把 guess 清干净」
    order = {"guess": 0, "auto": 1, "confirmed": 2, "rejected": 3}
    out.sort(key=lambda x: (order.get(x["status"], 9), -x["confidence"], x["term"]))
    return {"ok": True, "stats": lexicon.stats(c), "items": out}


@app.post("/api/lexicon/review")
async def api_lexicon_review(request: Request):
    """人在页面上做的三个动作：点头 / 驳回 / 改解释。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "body 不是 JSON"}
    term = str(body.get("term") or "").strip()
    action = str(body.get("action") or "").strip()
    gloss = str(body.get("gloss") or "").strip()
    if not term or action not in ("confirm", "reject", "gloss"):
        return {"ok": False, "error": "term / action 不对"}
    c = conn()
    if action == "confirm":
        n = lexicon.confirm(c, term, gloss)
    elif action == "reject":
        n = lexicon.reject(c, term)
    else:
        n = lexicon.correct(c, term, gloss)
    row = lexicon.get(c, term)
    return {"ok": bool(n), "n": n, "term": term,
            "item": None if not row else {"term": row["term"], "gloss": row["gloss"],
                                          "status": row["status"],
                                          "confidence": round(float(row["confidence"] or 0), 2)}}


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


@app.post("/api/curator/run")
def api_curator_run(force: bool = Query(False), dry: bool = Query(False)):
    """手动跑一轮选片。cron 也会直接跑 `python -m app.curator`。"""
    return curator.run(conn(), force=force, dry=dry)


@app.get("/api/curator/list")
def api_curator_list(limit: int = Query(20, ge=1, le=100)):
    return {"ok": True, "items": curator.history(conn(), limit=limit),
            "pending": len(curator.pending(limit=50))}


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
    现在库读不出来就 **503 + ok:false**：外部探活 / Cloudflare 健康检查只认状态码，
    返回 200 就等于「崩了也算健康」。异常细节只写日志，别把
    `sqlite3.OperationalError: unable to open database file /opt/...` 送给匿名访问者。
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
        print(f"[health] 库读不出来: {type(e).__name__}: {e}", flush=True)
        return JSONResponse(
            {"ok": False, "version": app.version, "db_ok": False, "error": "db unavailable"},
            status_code=503,
        )

# ------------------------------------------------------------------ B 站账号（v0.20）

_QR_PNG_CACHE: dict[str, bytes] = {}


@app.get("/bili", response_class=HTMLResponse)
def bili_page():
    """B 站运营页：扫码登录、看账号、审评论草稿、发出去。"""
    return HTMLResponse((WEB_DIR / "bili.html").read_text(encoding="utf-8"))


@app.get("/api/bili/accounts")
def api_bili_accounts():
    """两个账号各自的登录状态。main=刷视频，upload=投稿/评论。"""
    c = conn()
    out = []
    for a in bili.accounts(c):
        info = bili.me(c, a["account"])
        out.append({**a, "ok": info.get("ok"), "uname": info.get("uname"),
                    "mid": info.get("mid"), "level": info.get("level")})
    return {"ok": True, "items": out}


@app.get("/api/bili/me")
def api_bili_me(account: str = Query(bili.ACC_MAIN)):
    return bili.me(conn(), account)


@app.get("/api/bili/qr")
def api_bili_qr(account: str = Query(bili.ACC_UPLOAD)):
    return bili.qr_start(account)


@app.get("/api/bili/qr.png")
def api_bili_qr_png(url: str = Query(...)):
    """把二维码内容渲染成 PNG，前端 <img src> 直接用。"""
    if url in _QR_PNG_CACHE:
        return Response(_QR_PNG_CACHE[url], media_type="image/png")
    try:
        import qrcode
    except Exception:                                  # noqa: BLE001
        raise HTTPException(500, "服务器没装 qrcode 库")
    img = qrcode.make(url)
    import io as _io
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    _QR_PNG_CACHE[url] = data
    if len(_QR_PNG_CACHE) > 20:
        _QR_PNG_CACHE.clear()
    return Response(data, media_type="image/png")


@app.get("/api/bili/qr/poll")
def api_bili_qr_poll(key: str = Query(...), account: str = Query(bili.ACC_UPLOAD)):
    return bili.qr_poll(conn(), key, account)


@app.get("/api/bili/drafts")
def api_bili_drafts(status: str = Query("pending"), limit: int = Query(20, ge=1, le=100)):
    c = conn()
    return {"ok": True, "items": bili.drafts(c, status, limit), "stats": bili.stats(c),
            "posts_today": bili_writer.posts_today(c),
            "post_per_day": cfg.BILI_POST_PER_DAY}


@app.post("/api/bili/suggest")
def api_bili_suggest(bvid: str = Query(...), n: int = Query(3, ge=1, le=5)):
    """给某个视频生成评论草稿（只落草稿，不发出去）。"""
    return bili_writer.suggest(conn(), bvid, n)


@app.post("/api/bili/post")
def api_bili_post(id: int = Query(...)):
    """人工点头：把这条草稿真的发出去。"""
    c = conn()
    row = c.execute("SELECT * FROM bili_drafts WHERE id=?", (id,)).fetchone()
    if not row:
        raise HTTPException(404, "没有这条草稿")
    ok, why = bili_writer.can_post(c, row["aid"])
    if not ok:
        raise HTTPException(429, why)
    try:
        sent = bili.approve(c, id)
    except bili.BiliError as e:
        c.execute("UPDATE bili_drafts SET status='failed', error=? WHERE id=?", (str(e)[:300], id))
        c.commit()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "sent": sent}


@app.post("/api/bili/drop")
def api_bili_drop(id: int = Query(...)):
    bili.drop_draft(conn(), id)
    return {"ok": True}


@app.get("/api/bili/actions")
def api_bili_actions(limit: int = Query(20, ge=1, le=100)):
    c = conn()
    rows = c.execute("SELECT * FROM bili_actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"ok": True, "items": [dict(r) for r in rows]}
