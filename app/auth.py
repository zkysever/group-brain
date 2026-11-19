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


# 只在本地调试时用：.env 里写 GATE_DEV_OPEN=1 才允许「没配密码就人人都能进」。
# 以前这里直接 `if not enabled(): return True`——docstring 写着「宁可谁都进不来」，
# 代码却正好相反：GATE_PASSWORD 漏配（换机器、.env 打错、systemd 少了 EnvironmentFile）
# 会让 /admin、/api/agent（真烧钱）、/api/bili/post（真往 B 站发评论）全部匿名可用，
# 而且登录页还进不去（check_password 恒 False），连自己都关不上门。
_DEV_OPEN = os.environ.get("GATE_DEV_OPEN", "0").strip() in ("1", "true", "yes", "on")


def is_open(tok: str | None) -> bool:
    """没配密码时默认「谁都进不来」，只有显式 GATE_DEV_OPEN=1 才敞开。"""
    if not enabled():
        return _DEV_OPEN
    return verify_token(tok)


# 失败计数：ip -> 失败时间戳列表。进程内存，重启清零，够用了。
_fails: dict[str, list[float]] = {}
_PRUNE_AT = 500                  # 记满这么多 key 就整体剪一次，防止长期运行按 IP 泄漏内存


def _prune(now: float) -> None:
    dead = [k for k, v in _fails.items() if not v or now - v[-1] > _WINDOW]
    for k in dead:
        _fails.pop(k, None)


def too_many(ip: str) -> bool:
    now = time.time()
    if len(_fails) > _PRUNE_AT:
        _prune(now)
    hits = [t for t in _fails.get(ip, []) if now - t < _WINDOW]
    _fails[ip] = hits
    return len(hits) >= _LIMIT


def record_fail(ip: str) -> None:
    _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip: str) -> None:
    _fails.pop(ip, None)
