"""FastAPI 入口：网页 + 两个 JSON 接口。

启动：python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from . import db, search
from .config import MEME_PACKS_DIR, WEB_DIR

app = FastAPI(title="group-brain", version="0.1.0")

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
