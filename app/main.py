"""FastAPI 入口：网页 + 两个 JSON 接口 + 一道口令门。

启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

口令门：除了 /login、/api/health，所有路径都要带合法 cookie，否则
  - 页面 → 302 到 /login
  - /api/* → 401 JSON（前端收到就跳 /login）
"""
from __future__ import annotations

import mimetypes
import threading
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import auth, db, search
from .config import MEME_PACKS_DIR, WEB_DIR

app = FastAPI(title="group-brain", version="0.5.0")

# 不需要口令就能碰的路径。健康检查放开，方便外部探活 / 手机调试。
PUBLIC = {"/login", "/api/health", "/favicon.ico"}

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
