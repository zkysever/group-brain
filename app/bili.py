"""B 站账号操作：二维码登录、读账号、搜索、评论、点赞、关注。

设计约束（都是踩出来的）：
1. **不在聊天里要 cookie**。用官方扫码登录：`qr_generate()` 出二维码 → 用户手机扫 →
   `qr_poll()` 拿 Set-Cookie → 存进 `bili_auth` 表。密码与 cookie 都不经过对话。
2. **所有写操作默认只落草稿**。`add_comment()` 是唯一真正发出去的函数，
   只有 `approve()`（人工点头）才会调它；`BILI_AUTOPOST=1` 才由代码自己点。
3. **风控要认账**。B 站返回 `-352`（触发风控/需要验证码）、`-412`（请求被拦截）、
   `-101`（未登录）时，不重试、不当成功，把码原样记进 `bili_actions` 并让上层停下。
4. **wbi 签名**必须带：搜索、点赞、发评论这些接口从 2023 年起都看 `w_rid/wts`，
   不带就是 `-403` 或 `412`。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import requests

from . import config as cfg

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"
ORIGIN = "https://www.bilibili.com"

NAV = "https://api.bilibili.com/x/web-interface/nav"
VIEW = "https://api.bilibili.com/x/web-interface/view"
REPLIES = "https://api.bilibili.com/x/v2/reply"
ADD_REPLY = "https://api.bilibili.com/x/v2/reply/add"
LIKE = "https://api.bilibili.com/x/web-interface/archive/like"
RELATE = "https://api.bilibili.com/x/relation/modify"
SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# wbi 混淆表（bilibili-API-collect 里那张 64 位表，改动极小，多年未变）
_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
          33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
          61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
          11, 36, 20, 34, 44, 52]

RISK_CODES = {-352: "触发风控（要验证码）", -412: "请求被拦截", -101: "未登录", -111: "csrf 校验失败"}

# 两个账号分工（用户 2026-11-16 定的）：
#   main   —— 刷视频/选片，看到什么就推什么，不一定要登录
#   upload —— 投稿 + 发评论（另一个号，专门放视频和说话）
ACC_MAIN = "main"
ACC_UPLOAD = "upload"


class BiliError(RuntimeError):
    pass


def _buvid() -> str:
    return (f"{random.getrandbits(32):08x}-{random.getrandbits(16):04x}-"
            f"{random.getrandbits(16):04x}-{random.getrandbits(16):04x}-"
            f"{random.getrandbits(48):012x}infoc")


# 每个账号一条独立的会话：qr_generate 会给会话种 buvid3/buvid4，
# 确认登录后 SESSDATA 也发到这个会话上。之前每次请求换随机 buvid3，
# 结果就是「登录成功但没拿到 SESSDATA」。
_QR_SESSIONS: dict[str, requests.Session] = {}
_QR_LAST: dict[str, dict] = {}


def _qr(account: str = ACC_MAIN) -> requests.Session:
    s = _QR_SESSIONS.get(account)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Referer": REFERER,
            "Origin": ORIGIN,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        _QR_SESSIONS[account] = s
    return s


def _qr_cookie_pairs(account: str = ACC_MAIN) -> dict:
    """把会话里的 cookie 全取出来（requests 已按域整理好）。"""
    return {c.name: c.value for c in _qr(account).cookies if c.value}


def _headers(cookie: str = "", ajax: bool = False) -> dict:
    h = {
        "User-Agent": UA,
        "Referer": REFERER,
        "Origin": ORIGIN,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if ajax:
        h["X-Requested-With"] = "XMLHttpRequest"
    if cookie:
        h["Cookie"] = cookie
    else:
        h["Cookie"] = "buvid3=" + _buvid()
    return h


def _request(url: str, params: dict | None = None, data: dict | None = None,
             cookie: str = "", ajax: bool = False, timeout: float = 20.0):
    """返回 (json, headers)。headers 小写键。"""
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=body, headers=_headers(cookie, ajax))
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
    if not raw.strip():
        return {}, hdrs
    return json.loads(raw), hdrs


# ---------------------------------------------------------------- cookie 存取

def _ckey(account: str) -> str:
    return f"cookie:{account or ACC_MAIN}"


def load_cookie(conn, account: str = ACC_MAIN) -> str:
    """从 bili_auth 表读某个账号的 cookie 串；没有就退回环境变量。"""
    if conn is not None:
        try:
            for k in (_ckey(account), "cookie" if account in ("", ACC_MAIN) else None):
                if not k:
                    continue
                row = conn.execute("SELECT v FROM bili_auth WHERE k=?", (k,)).fetchone()
                if row and row["v"]:
                    return row["v"]
        except Exception:                              # noqa: BLE001
            pass
    return (cfg.BILI_COOKIE or "") if account in ("", ACC_MAIN) else ""


def save_cookie(conn, pairs: dict, account: str = ACC_MAIN) -> str:
    """把登录返回的 cookie 键值存起来（保留已有的 buvid 等）。"""
    old = {}
    for part in load_cookie(conn, account).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            old[k.strip()] = v.strip()
    old.update({k: v for k, v in pairs.items() if v})
    cookie = "; ".join(f"{k}={v}" for k, v in old.items() if v)
    conn.execute("INSERT INTO bili_auth(k, v, ts) VALUES(?, ?, ?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
                 (_ckey(account), cookie, _now()))
    conn.commit()
    return cookie


def clear_cookie(conn, account: str = ACC_MAIN) -> None:
    conn.execute("DELETE FROM bili_auth WHERE k=?", (_ckey(account),))
    conn.commit()


def accounts(conn) -> list[dict]:
    """两个账号各自的登录状态，给页面显示。"""
    out = []
    for acc in (ACC_MAIN, ACC_UPLOAD):
        ck = load_cookie(conn, acc)
        out.append({"account": acc, "has_cookie": bool(ck),
                    "roles": "刷视频/选片" if acc == ACC_MAIN else "投稿/评论"})
    return out


def _csrf(cookie: str) -> str:
    m = re.search(r"bili_jct=([^;]+)", cookie)
    if not m:
        raise BiliError("cookie 里没有 bili_jct（csrf），发不了写操作")
    return m.group(1)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- wbi

def _wbi_key(cookie: str) -> str:
    d, _ = _request(NAV, cookie=cookie)
    img = ((d.get("data") or {}).get("wbi_img") or {})
    raw = re.sub(r"^.*/|\.\w+$", "", img.get("img_url", "")) + \
        re.sub(r"^.*/|\.\w+$", "", img.get("sub_url", ""))
    if len(raw) < 64:
        raise BiliError("拿不到 wbi 密钥（nav 接口没返回 wbi_img）")
    return "".join(raw[i] for i in _MIXIN)[:32]


def _sign(params: dict, key: str) -> dict:
    p = dict(params)
    p["wts"] = int(time.time())
    q = urllib.parse.urlencode(sorted(p.items()))
    p["w_rid"] = hashlib.md5((q + key).encode()).hexdigest()
    return p


# ---------------------------------------------------------------- 读

def me(conn, account: str = ACC_MAIN) -> dict:
    cookie = load_cookie(conn, account)
    d, _ = _request(NAV, cookie=cookie)
    data = d.get("data") or {}
    return {
        "ok": d.get("code") == 0 and bool(data.get("isLogin")),
        "code": d.get("code"),
        "message": d.get("message"),
        "account": account,
        "uname": data.get("uname"),
        "mid": data.get("mid"),
        "level": data.get("level_info", {}).get("current_level"),
        "coins": data.get("money"),
        "vip": (data.get("vipStatus") == 1),
        "fans": None,          # 粉丝数要另外调 relation/stat，登录后才准
        "logged_in": bool(cookie),
    }


def view(bvid: str, conn=None) -> dict:
    cookie = load_cookie(conn) if conn is not None else ""
    d, _ = _request(VIEW, {"bvid": bvid}, cookie=cookie)
    dd = d.get("data") or {}
    return {"code": d.get("code"), "aid": dd.get("aid"), "bvid": bvid,
            "title": dd.get("title"), "owner": (dd.get("owner") or {}).get("name"),
            "mid": (dd.get("owner") or {}).get("mid"),
            "desc": (dd.get("desc") or "")[:600], "duration": dd.get("duration"),
            "stat": dd.get("stat") or {}, "cid": dd.get("cid"),
            "pubdate": dd.get("pubdate")}


def replies(aid: int, limit: int = 10, conn=None) -> list[dict]:
    cookie = load_cookie(conn) if conn is not None else ""
    d, _ = _request(REPLIES, {"type": 1, "oid": aid, "sort": 2, "ps": limit, "pn": 1},
                    cookie=cookie)
    out = []
    for r in ((d.get("data") or {}).get("replies") or [])[:limit]:
        out.append({"rpid": r.get("rpid"), "like": r.get("like"),
                    "user": (r.get("member") or {}).get("uname"),
                    "text": (r.get("content") or {}).get("message", "")[:200]})
    return out


def search(keyword: str, limit: int = 8, conn=None) -> list[dict]:
    cookie = load_cookie(conn) if conn is not None else ""
    key = _wbi_key(cookie)
    p = _sign({"search_type": "video", "keyword": keyword, "page": 1}, key)
    d, _ = _request(SEARCH, p, cookie=cookie)
    out = []
    for v in ((d.get("data") or {}).get("result") or [])[:limit]:
        out.append({"bvid": v.get("bvid"),
                    "title": re.sub(r"<[^>]+>", "", v.get("title") or ""),
                    "author": v.get("author"), "play": v.get("play")})
    return out


# ---------------------------------------------------------------- 二维码登录

def qr_start(account: str = ACC_MAIN) -> dict:
    """出二维码。会话里会带上 buvid3/buvid4，后续轮询必须用同一个会话。"""
    r = _qr(account).get(QR_GEN, timeout=20)
    d = r.json()
    dd = d.get("data") or {}
    _QR_LAST[account] = {"cookies_at_start": sorted(_qr_cookie_pairs(account))}
    return {"code": d.get("code"), "url": dd.get("url"),
            "key": dd.get("qrcode_key"), "account": account}


def qr_poll(conn, key: str, account: str = ACC_MAIN) -> dict:
    r = _qr(account).get(QR_POLL, params={"qrcode_key": key}, timeout=20)
    try:
        d = r.json()
    except Exception:                                  # noqa: BLE001
        return {"code": None, "status": "unknown", "message": "poll 返回的不是 JSON"}

    dd = d.get("data") or {}
    st = dd.get("code")
    _QR_LAST.setdefault(account, {}).update({
        "poll_keys": sorted(dd.keys()),
        "poll_code": st,
        "poll_msg": dd.get("message"),
        "has_set_cookie": bool(r.headers.get("set-cookie")),
        "jar": sorted(_qr_cookie_pairs(account)),
    })

    # 86101 未扫码 / 86090 已扫码未确认 / 86038 已失效 / 0 成功
    if st == 0:
        pairs = _qr_cookie_pairs(account)
        if not pairs.get("SESSDATA"):
            return {"code": 0, "status": "no_cookie",
                    "message": "登录成功但没拿到 SESSDATA",
                    "debug": dict(_QR_LAST.get(account) or {})}
        save_cookie(conn, pairs, account)
        info = me(conn, account)
        return {"code": 0, "status": "ok", "account": account,
                "uname": info.get("uname"), "mid": info.get("mid"),
                "saved": sorted(pairs)}
    return {"code": st, "status": {86101: "waiting", 86090: "scanned",
                                   86038: "expired"}.get(st, "unknown"),
            "message": dd.get("message")}


# ---------------------------------------------------------------- 写（谨慎）

def _audit(conn, action: str, target: str, ok: bool, code, detail: str) -> None:
    try:
        conn.execute("INSERT INTO bili_actions(ts, action, target, ok, code, detail) "
                     "VALUES(?,?,?,?,?,?)",
                     (_now(), action, str(target), 1 if ok else 0,
                      None if code is None else int(code), (detail or "")[:500]))
        conn.commit()
    except Exception as e:                          # noqa: BLE001
        print(f"[warn] 审计日志没写进去: {e}")


def add_comment(conn, aid: int, text: str, bvid: str = "",
                account: str = ACC_UPLOAD) -> dict:
    """真正发一条评论。调用方必须是「人工点头」或显式开启的自动模式。

    评论默认用 upload 账号（专门说话的那个），不要拿刷视频的号去留言。
    """
    text = (text or "").strip()
    if not (2 <= len(text) <= 500):
        raise BiliError(f"评论长度不对（{len(text)} 字，要求 2–500）")
    cookie = load_cookie(conn, account)
    if not cookie:
        raise BiliError(f"账号 {account} 没登录，先扫码")
    csrf = _csrf(cookie)
    key = _wbi_key(cookie)
    params = _sign({"oid": aid, "type": 1, "message": text, "plat": 1, "csrf": csrf}, key)
    try:
        d, _ = _request(ADD_REPLY, data=params, cookie=cookie, ajax=True)
    except urllib.error.HTTPError as e:
        _audit(conn, "comment", bvid or aid, False, e.code, f"HTTP {e.code}")
        raise BiliError(f"发评论 HTTP {e.code}") from e
    code = d.get("code")
    ok = code == 0
    detail = "ok" if ok else f"{code}: {d.get('message')}"
    _audit(conn, "comment", bvid or aid, ok, code, detail)
    if not ok:
        hint = RISK_CODES.get(code)
        raise BiliError(f"发评论失败 {detail}" + (f"（{hint}）" if hint else ""))
    rpid = ((d.get("data") or {}).get("rpid"))
    return {"ok": True, "rpid": rpid, "aid": aid}


def like(conn, bvid: str, on: bool = True) -> dict:
    cookie = load_cookie(conn)
    csrf = _csrf(cookie)
    d, _ = _request(LIKE, data={"bvid": bvid, "like": 1 if on else 2, "csrf": csrf},
                    cookie=cookie, ajax=True)
    ok = d.get("code") == 0
    _audit(conn, "like", bvid, ok, d.get("code"), d.get("message") or "")
    return {"ok": ok, "code": d.get("code"), "message": d.get("message")}


def follow(conn, mid: int, on: bool = True) -> dict:
    cookie = load_cookie(conn)
    csrf = _csrf(cookie)
    d, _ = _request(RELATE, data={"fid": mid, "act": 1 if on else 2,
                                  "re_src": 11, "csrf": csrf}, cookie=cookie, ajax=True)
    ok = d.get("code") == 0
    _audit(conn, "follow", mid, ok, d.get("code"), d.get("message") or "")
    return {"ok": ok, "code": d.get("code"), "message": d.get("message")}


# ---------------------------------------------------------------- 草稿

def add_draft(conn, bvid: str, text: str, reason: str = "", aid: int | None = None,
              title: str = "", account: str = ACC_UPLOAD) -> int:
    if aid is None:
        v = view(bvid, conn)
        aid, title = v.get("aid"), v.get("title") or title
    cur = conn.execute(
        "INSERT INTO bili_drafts(created_at, bvid, aid, title, text, reason, status, account) "
        "VALUES(?,?,?,?,?,?, 'pending', ?)",
        (_now(), bvid, aid, (title or "")[:200], text.strip(), (reason or "")[:300], account))
    conn.commit()
    return int(cur.lastrowid)


def drafts(conn, status: str = "pending", limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM bili_drafts WHERE status = ? ORDER BY id DESC LIMIT ?",
        (status, limit)).fetchall()
    return [dict(r) for r in rows]


def drop_draft(conn, did: int) -> None:
    conn.execute("UPDATE bili_drafts SET status='dropped' WHERE id=?", (did,))
    conn.commit()


def approve(conn, did: int) -> dict:
    """人工点头：把这一条草稿真的发出去（用草稿上记的账号）。"""
    row = conn.execute("SELECT * FROM bili_drafts WHERE id=?", (did,)).fetchone()
    if not row:
        raise BiliError(f"没有草稿 {did}")
    if row["status"] != "pending":
        raise BiliError(f"草稿 {did} 状态是 {row['status']}，不重复发")
    acc = (dict(row).get("account") or ACC_UPLOAD)
    sent = add_comment(conn, row["aid"], row["text"], row["bvid"], account=acc)
    conn.execute("UPDATE bili_drafts SET status='posted', posted_at=?, rpid=? WHERE id=?",
                 (_now(), sent.get("rpid"), did))
    conn.commit()
    return sent


def stats(conn) -> dict:
    try:
        rows = conn.execute("SELECT status, COUNT(*) c FROM bili_drafts GROUP BY status").fetchall()
        d = {r["status"]: r["c"] for r in rows}
        acts = conn.execute("SELECT COUNT(*) c FROM bili_actions").fetchone()["c"]
        return {"pending": d.get("pending", 0), "posted": d.get("posted", 0),
                "dropped": d.get("dropped", 0), "actions": acts,
                "logged_in": bool(load_cookie(conn, ACC_MAIN)),
                "logged_in_upload": bool(load_cookie(conn, ACC_UPLOAD))}
    except Exception:                              # noqa: BLE001
        return {"pending": 0, "posted": 0, "dropped": 0, "actions": 0,
                "logged_in": False, "logged_in_upload": False}
