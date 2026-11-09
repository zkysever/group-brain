"""FastAPI 入口：网页 + 两个 JSON 接口 + 一道口令门。

启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

口令门：除了 /login、/api/health，所有路径都要带合法 cookie，否则
  - 页面 → 302 到 /login
  - /api/* → 401 JSON（前端收到就跳 /login）
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import auth, db, search
from .config import MEME_PACKS_DIR, WEB_DIR

app = FastAPI(title="group-brain", version="0.1.0")

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

_conn = None


def conn():
    """一个进程一条连接，够用了。真并发上来了再换连接池。"""
    global _conn
    if _conn is None:
        _conn = db.connect()
    return _conn


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
):
    q = q.strip()
    if not q:
        return {"query": "", "kind": kind, "took_ms": 0, "count": 0, "hits": []}
    return search.search_all(conn(), q, kind, limit)


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
    return {"ok": True, "version": app.version}
