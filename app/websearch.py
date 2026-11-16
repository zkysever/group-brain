"""联网检索：给 Agent 一个「自己上网找资料」的工具。

为什么要有这一层
----------------
群聊记忆体只知道自己那 572 条消息。问「Python 3.12 什么时候发布的」「FastAPI 现在什么版本」，
它以前只能说「群里没提过」。这不是知识问题，是**它没有眼睛**。

选型（2026-11-16 在这台机器和杭州那台服务器上都实测过）
------------------------------------------------------
| 通道 | 本机 | 服务器 | 要 key |
|---|---|---|---|
| cn.bing.com 搜索页 | 200 / 1.1s | 200 / 0.3s | 否 |  ← 兜底，默认
| 百度搜索页 | 200 / 1.2s | 302（风控跳转） | 否 |  ✗ 不稳
| html.duckduckgo.com | 000 超时 | 000 超时 | 否 |  ✗ 国内不通
| api.duckduckgo.com | — | 000 超时 | 否 |  ✗
| api.tavily.com | 200 | 200 | 是 |  ← 有 key 就优先用它
| api.search.brave.com | 000 | 000 | 是 |  ✗ 不通
| zh.wikipedia.org | 000 | 000 | 否 |  ✗ 不通

所以默认走「抓 Bing 中文结果页」。这条路没有合同、没有 SLO，Bing 改版就会断，
因此：
1. 解析失败一律返回空列表，绝不抛异常把整轮对话弄挂（和 facts 的 0 结果同一个原则）。
2. `TAVILY_API_KEY` 存在时优先用 Tavily（结构化 JSON、质量更稳），Bing 只做兜底。
3. 结果缓存进 `web_cache` 表，同一个问题一小时内不再打网络。

只提供「搜索 + 只抓一个页面」：不提供批量抓正文。正文动不动几万字，塞进上下文既贵又容易带偏，
所以 `fetch()` 只取页面开头 1200 字，够回答「版本号是多少」「这个参数怎么用」这类具体问题，
剩下的交给用户在浏览器里点链接。

真浏览器兜底（2026-11-16 加的）
-----------------------------
纯 HTTP 抓不到三类页面：403（知乎 / StackOverflow 的 CF 盾）、只有 JS 壳（掘金 200 但只
2397 字节）、302 到登录页（小红书）。这条链路上有个「真浏览器阅读服务」跑在主人那台
Windows 上（`Desktop\bsk-reader\bsk_reader.py`，驱动他自己登录态的 Chromium），再通过
`ssh -N -R 8010:127.0.0.1:8010 gb` 反向隧道暴露给服务器，`fetch()` 抓不到时才转发过去。
电脑关着 / 隧道没开时，`_reader_health()` 2 秒超时失败，行为与以前完全一样。
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import sqlite3
import time
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BING = "https://cn.bing.com/search"
TAVILY = "https://api.tavily.com/search"
CACHE_TTL = 3600  # 秒；同一问题一小时内复用
MAX_SNIPPET = 220


class WebError(RuntimeError):
    pass


def _tavily_key() -> str:
    return (os.environ.get("TAVILY_API_KEY") or "").strip()


def available() -> bool:
    """永远是 True：Bing 那条路不需要 key。留这个函数是为了和 agent.available() 对齐。"""
    return True


def _strip(fragment: str) -> str:
    """去标签 + 反转义 + 压空白。Bing 的标题里常带 <strong> 高亮。"""
    text = re.sub(r"<[^>]+>", "", fragment or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_bing(page: str, limit: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    # Bing 的自然结果块是 <li class="b_algo">…；每个块里 h2>a 是标题+链接，第一个 p 是摘要
    for chunk in page.split('<li class="b_algo"')[1:]:
        chunk = chunk[:6000]
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        url, title = m.group(1), _strip(m.group(2))
        if not url.startswith("http") or not title:
            continue
        sn = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        snippet = _strip(sn.group(1))[:MAX_SNIPPET] if sn else ""
        out.append({"title": title[:120], "url": url, "snippet": snippet, "source": "bing"})
        if len(out) >= limit:
            break
    return out


def _bing(query: str, limit: int, timeout: float = 12.0) -> list[dict[str, str]]:
    params = urllib.parse.urlencode({"q": query, "setlang": "zh-CN", "ensearch": "0"})
    req = urllib.request.Request(
        f"{BING}?{params}",
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        page = resp.read().decode("utf-8", "replace")
    return _parse_bing(page, limit)


def _tavily(query: str, limit: int, timeout: float = 20.0) -> list[dict[str, str]]:
    body = json.dumps(
        {"api_key": _tavily_key(), "query": query, "max_results": limit, "search_depth": "basic"}
    ).encode("utf-8")
    req = urllib.request.Request(
        TAVILY,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    out = []
    for r in data.get("results") or []:
        out.append(
            {
                "title": _strip(r.get("title") or "")[:120],
                "url": r.get("url") or "",
                "snippet": _strip(r.get("content") or "")[:MAX_SNIPPET],
                "source": "tavily",
            }
        )
    return out[:limit]


def _cache_get(conn: sqlite3.Connection | None, key: str) -> list[dict[str, str]] | None:
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT hits, ts FROM web_cache WHERE qkey = ?", (key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # 老库还没这张表
    if not row:
        return None
    if time.time() - float(row["ts"] or 0) > CACHE_TTL:
        return None
    try:
        return json.loads(row["hits"])
    except json.JSONDecodeError:
        return None


def _cache_put(conn: sqlite3.Connection | None, key: str, hits: list[dict[str, str]]) -> None:
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO web_cache (qkey, q, hits, ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(qkey) DO UPDATE SET hits = excluded.hits, ts = excluded.ts",
            (key, key, json.dumps(hits, ensure_ascii=False), time.time()),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _key(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip().lower())[:200]


def search(conn=None, query: str = "", limit: int = 5, use_cache: bool = True) -> list[dict[str, str]]:
    """搜一下网，返回 [{title, url, snippet, source}]。

    任何一层失败都不抛异常——最坏是返回空列表，让模型自己说「没找到」。
    """
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit or 5), 8))
    key = _key(q)
    if use_cache:
        cached = _cache_get(conn, key)
        if cached is not None:
            return cached[:limit]

    hits: list[dict[str, str]] = []
    errors: list[str] = []
    if _tavily_key():
        try:
            hits = _tavily(q, limit)
        except Exception as exc:  # noqa: BLE001 —— 联网这层不能把对话弄挂
            errors.append(f"tavily: {type(exc).__name__}: {exc}")
    if not hits:
        try:
            hits = _bing(q, limit)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"bing: {type(exc).__name__}: {exc}")
    if errors and not hits:
        print("[warn] 联网检索失败: " + " | ".join(errors), flush=True)
    if hits and use_cache:
        _cache_put(conn, key, hits)
    return hits


def stats(conn) -> dict[str, int]:
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM web_cache").fetchone()["n"]
    except sqlite3.OperationalError:
        n = 0
    return {"cached": int(n or 0), "tavily": 1 if _tavily_key() else 0}


# ---------------------------------------------------------------- 抓单个页面
MAX_PAGE = 1200


def _safe_target(url: str) -> str:
    """只放行公网 http(s)。

    这不是洁癖：服务器上跑着 AstrBot、Docker、内网 172.22.x.x，
    如果模型被诱导去 fetch http://127.0.0.1:8000/api/admin/run 就有得玩了。
    所以：协议只认 http/https，端口只认 80/443/空，域名解析出来的 IP 必须在公网段。
    """
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise WebError(f"只支持 http/https: {p.scheme}")
    if p.port and p.port not in (80, 443):
        raise WebError(f"只支持 80/443 端口: {p.port}")
    host = p.hostname or ""
    if not host:
        raise WebError("URL 里没有域名")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise WebError(f"解析不了域名: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise WebError(f"{host} 解析到内网地址 {ip}，不抓")
    return url


def _page_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page)
    text = _strip(re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h1>|</h2>", "\n", page))
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def fetch(url: str, max_chars: int = MAX_PAGE, timeout: float = 12.0) -> dict[str, str]:
    """抓一个页面的可读文本（前 max_chars 字）。

    先走纯 HTTP；抓不到（403 / 空正文 / 只有 JS 壳）再问真浏览器兜底一次。
    两条都不行才抛 WebError，由调用方兜成「抓不到」。
    """
    target = _safe_target(url)
    try:
        data = _plain_fetch(target, max_chars, timeout)
        if len(data["text"]) >= MIN_PAGE_CHARS:
            data["via"] = "http"
            return data
        thin = data
    except Exception as exc:  # 403 / 超时 / 编码异常统统走兜底
        thin = {"title": "", "url": target, "text": "", "err": f"{type(exc).__name__}: {exc}"}

    browser = browser_read(target, max_chars=max_chars)
    if browser:
        return browser
    if thin.get("text"):
        thin["via"] = "http"
        return thin
    raise WebError(thin.get("err") or "抓不到页面正文")


def _plain_fetch(url: str, max_chars: int, timeout: float) -> dict[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(400_000)
        charset = resp.headers.get_content_charset() or "utf-8"
    page = raw.decode(charset, "replace")
    found = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    title = _strip(found.group(1)) if found else ""
    return {"title": title[:120], "url": url, "text": _page_text(page)[:max_chars]}


# ------------------------------------------------------- 真浏览器兜底（本机阅读服务）
MIN_PAGE_CHARS = 200
READER_TIMEOUT = 25.0
_reader_state = {"at": 0.0, "ok": False}
_READER_TTL = 30.0


def browser_reader() -> str:
    try:
        from . import config as cfg

        return getattr(cfg, "BROWSER_READER", "") or ""
    except Exception:
        return os.environ.get("BROWSER_READER_URL", "").strip()


def _reader_health() -> bool:
    """2 秒内探一下隧道通不通，结果缓存 30 秒——隧道不在时不要每次都白等。"""
    base = browser_reader()
    if not base:
        return False
    now = time.time()
    if now - _reader_state["at"] < _READER_TTL:
        return bool(_reader_state["ok"])
    ok = False
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=2.0) as resp:
            ok = json.loads(resp.read().decode("utf-8", "replace")).get("ok") is True
    except Exception:
        ok = False
    _reader_state.update({"at": now, "ok": ok})
    return ok


def browser_read(url: str, max_chars: int = MAX_PAGE) -> dict[str, str] | None:
    """用真浏览器读页面。隧道不在 / 读不到都返回 None，绝不抛异常。"""
    base = browser_reader()
    if not base or not _reader_health():
        return None
    api = base.rstrip("/") + "/read?url=" + urllib.parse.quote(url, safe="")
    try:
        with urllib.request.urlopen(api, timeout=READER_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        print(f"[warn] 真浏览器兜底失败: {type(exc).__name__}: {exc}")
        return None
    if not data.get("ok"):
        print(f"[warn] 真浏览器没读到: {str(data.get('error'))[:120]}")
        return None
    text = (data.get("text") or "")[:max_chars]
    if len(text) < MIN_PAGE_CHARS:
        return None
    return {"title": "", "url": url, "text": text, "via": "browser"}
