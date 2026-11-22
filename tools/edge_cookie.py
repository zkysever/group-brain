"""从本机 Edge 配置里取出 bilibili 的登录 cookie（SESSDATA / bili_jct / DedeUserID）。

为什么要这么干：B 站把 SESSDATA 设成 HttpOnly，浏览器里用 JS 写不进去，
而 bsk 的 Agent 窗口是独立（incognito）profile，拿不到你日常登录的会话。
所以你每次扫码都只够一个会话用。改成直接读浏览器自己的 cookie 存起来，
之后发布就不用再扫了。

只读本机文件，不上传任何东西；输出只打印 cookie 名字和长度，不打印值。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sqlite3
import sys
import tempfile

EDGE_USER = os.path.join(os.environ["LOCALAPPDATA"], r"Microsoft\Edge\User Data")
SECRETS = os.path.join(os.path.expanduser("~"), ".dsh", "secrets")
WANT = ("SESSDATA", "bili_jct", "DedeUserID")


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi(blob: bytes) -> bytes:
    buf_in = DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char)))
    buf_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(buf_in), None, None, None, None, 0, ctypes.byref(buf_out)
    ):
        raise OSError("CryptUnprotectData 失败")
    return ctypes.string_at(buf_out.pbData, buf_out.cbData)


def aes_key(profile_root: str) -> bytes:
    with open(os.path.join(profile_root, "Local State"), "r", encoding="utf-8") as f:
        state = json.load(f)
    enc = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if enc[:5] != b"DPAPI":
        raise ValueError("encrypted_key 不是 DPAPI 格式")
    return dpapi(enc[5:])


def decrypt(value: bytes, key: bytes) -> str:
    if value[:3] in (b"v10", b"v20"):
        nonce, body = value[3:15], value[15:]
        ct, tag = body[:-16], body[-16:]
        # AES-256-GCM，用 ctypes 直接调 bcrypt 太啰嗦，退回 cryptography（若装了）
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ModuleNotFoundError as e:  # pragma: no cover
            raise SystemExit("需要 cryptography：.venv\\Scripts\\pip install cryptography") from e
        return AESGCM(key).decrypt(nonce, ct + tag, None).decode("utf-8", "replace")
    return dpapi(value).decode("utf-8", "replace")


def read_cookies(profile: str, db_override: str = "") -> dict[str, str]:
    db = db_override or os.path.join(EDGE_USER, profile, "Network", "Cookies")
    if not os.path.exists(db):
        raise SystemExit(f"找不到 cookie 库：{db}（Edge 没在这个 profile 登录过？）")
    # Edge 正在运行时文件被独占锁住，sqlite 连不上、复制也报 WinError 32；
    # 先按字节读进来（共享读通常允许），再 deserialize 成一个内存库来查。
    rows = None
    try:
        with open(db, "rb") as f:
            raw = f.read()
        mem = sqlite3.connect(":memory:")
        mem.deserialize(raw)
        rows = mem.execute(
            "SELECT host_key, name, value, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%bilibili.com'"
        ).fetchall()
        mem.close()
    except (OSError, sqlite3.Error) as e:
        print(f"  [warn] 直读失败（{type(e).__name__}: {e}），改用 immutable 打开")
        try:
            uri = __import__("pathlib").Path(db).as_uri() + "?immutable=1"
            con = sqlite3.connect(uri, uri=True)
            rows = con.execute(
                "SELECT host_key, name, value, encrypted_value FROM cookies "
                "WHERE host_key LIKE '%bilibili.com'"
            ).fetchall()
            con.close()
        except sqlite3.Error as e2:
            raise SystemExit(f"读 cookie 库失败：{e2}（试试先关掉 Edge）") from e2
    out: dict[str, str] = {}
    if True:
        key = None
        for host, name, plain, enc in rows:
            if name not in WANT:
                continue
            if plain:
                out[name] = plain
                continue
            if key is None:
                key = aes_key(EDGE_USER)
            try:
                out[name] = decrypt(enc, key)
            except Exception as e:  # noqa: BLE001
                print(f"  [{name}] 解密失败：{type(e).__name__}: {e}")
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    db_override = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--db=")), "")
    profile = args[0] if args else "Default"
    got = read_cookies(profile, db_override)
    if not got:
        print(f"[x] {profile} 里没读到 bilibili 的登录 cookie（Edge 里没登录？）")
        return 1
    print(f"[ok] profile={profile} 读到：")
    for k in WANT:
        v = got.get(k, "")
        print(f"  {k:10s} {'有' if v else '缺'}  {len(v)} 字符")
    if not all(got.get(k) for k in ("SESSDATA", "bili_jct")):
        print("[x] SESSDATA / bili_jct 不全，没法用来发布")
        return 1

    os.makedirs(SECRETS, exist_ok=True)
    cookie = "; ".join(f"{k}={got[k]}" for k in WANT if got.get(k))
    path = os.path.join(SECRETS, "bili-main-cookie.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(cookie + "\n")
    print(f"[ok] 已写入 {path}（{len(cookie)} 字符）")

    # 立刻验证这是谁
    import urllib.request
    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    data = d.get("data") or {}
    print(f"[ok] isLogin={data.get('isLogin')} uname={data.get('uname')} mid={data.get('mid')}")
    return 0 if data.get("isLogin") else 2


if __name__ == "__main__":
    raise SystemExit(main())
