"""一道口令门：一个密码换一个签名 cookie。

为什么不用 Cloudflare Access：那套要邮箱验证码，用户明确说不要。
为什么不用现成的 session 库：这道门只要八十行标准库，加依赖反而是负担。

设计取舍：
- 密码从环境变量 GATE_PASSWORD 读，不写进代码、不进 git；
- cookie 不是随意字符串，是 `过期时间.HMAC-SHA256(过期时间)`，
  改一个字节就失效，伪造要先拿到 SECRET；
- SECRET 存在 data/.gate_secret（0600），服务重启后会话不失效；
- 密码比对用 hmac.compare_digest，避免计时侧信道（虽然对这道门有点小题大做）；
- 同一 IP 十分钟内错十次就先挡住，防脚本爆库。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from .config import DATA_DIR

COOKIE_NAME = "gb_gate"
MAX_AGE = 7 * 24 * 3600          # 7 天内不用再输
_WINDOW = 600                    # 失败计数窗口：10 分钟
_LIMIT = 10                      # 窗口内最多错十次


def _load_secret() -> bytes:
    env = os.environ.get("GATE_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    f = DATA_DIR / ".gate_secret"
    if f.is_file() and f.stat().st_size:
        return f.read_bytes().strip()
    s = secrets.token_hex(32)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(s, encoding="utf-8")
    try:
        f.chmod(0o600)
    except OSError:
        pass
    return s.encode("utf-8")


SECRET = _load_secret()
PASSWORD = os.environ.get("GATE_PASSWORD", "").strip()


def enabled() -> bool:
    """没配密码就当门关着——宁可谁都进不来，也不能默认敞开。"""
    return bool(PASSWORD)


def check_password(pw: str) -> bool:
    if not enabled():
        return False
    return hmac.compare_digest(pw.encode("utf-8"), PASSWORD.encode("utf-8"))


def _sign(exp: int) -> str:
    mac = hmac.new(SECRET, str(exp).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def issue_token() -> str:
    return _sign(int(time.time()) + MAX_AGE)


def verify_token(tok: str | None) -> bool:
    if not tok or "." not in tok:
        return False
    exp_s, _, mac = tok.partition(".")
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    good = hmac.new(SECRET, exp_s.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, good)


def is_open(tok: str | None) -> bool:
    """门开着（没配密码）或 cookie 合法，都算放行。"""
    if not enabled():
        return True
    return verify_token(tok)


# 失败计数：ip -> 失败时间戳列表。进程内存，重启清零，够用了。
_fails: dict[str, list[float]] = {}


def too_many(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < _WINDOW]
    _fails[ip] = hits
    return len(hits) >= _LIMIT


def record_fail(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip: str) -> None:
    _fails.pop(ip, None)
