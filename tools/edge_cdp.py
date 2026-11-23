"""用一个「记得住登录」的 Edge 配置目录来投稿。

跟 bsk 的区别：bsk 的 Agent Window 是无痕 profile，关掉就没了，所以每次都要扫码。
这里用一个固定的 --user-data-dir（C:\\Users\\Administrator\\.dsh\\edge-pub），
登录态存在磁盘上，扫一次就一直有效。

用法：
    python tools/edge_cdp.py launch         # 起窗口（已开则不重复起）
    python tools/edge_cdp.py whoami         # 看这个窗口现在登录的是谁
    python tools/edge_cdp.py eval "<js>"    # 页面里跑一段 JS，打印结果
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROFILE = Path(os.path.expandvars(r"%USERPROFILE%\.dsh\edge-pub"))
PORT = 9223
UPLOAD_URL = "https://member.bilibili.com/platform/upload/video/frame"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def edge_exe() -> str:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("没找到 msedge.exe")


def _http(path: str, timeout: float = 5.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def cdp_alive() -> bool:
    try:
        _http("/json/version")
        return True
    except Exception:
        return False


def launch(url: str = UPLOAD_URL, wait: float = 6.0) -> None:
    """起窗口。profile 目录是持久的，所以登录态会留着。"""
    if cdp_alive():
        print(f"[edge] 已经在跑（端口 {PORT}）")
        return
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        # 这个 profile 第一次启动时踩过 ERR_PROXY_CONNECTION_FAILED
        # （系统代理指向一个没在跑的本机端口），加这个就顺了
        "--no-proxy-server",
        url,
    ]
    subprocess.Popen([edge_exe(), *args], close_fds=True)
    for _ in range(int(wait * 4)):
        time.sleep(0.25)
        if cdp_alive():
            print(f"[edge] 起来了：profile={PROFILE} port={PORT}")
            return
    raise SystemExit("[edge] 6 秒内没等到 CDP 端口，可能被杀软拦了")


def page_target() -> dict:
    """挑一个可用的 page 目标；优先 bilibili 的。"""
    tabs = [t for t in _http("/json/list") if t.get("type") == "page"]
    if not tabs:
        raise SystemExit("[edge] 没有可用的标签页")
    for t in tabs:
        if "bilibili" in (t.get("url") or ""):
            return t
    return tabs[0]


def call(method: str, params: dict, timeout: float = 60.0):
    """调一个不需要返回值的 CDP 方法（Page.navigate 之类）。"""
    from websocket import create_connection

    t = page_target()
    ws = create_connection(t["webSocketDebuggerUrl"], timeout=timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == 1:
                return data.get("result")
    finally:
        ws.close()


def js(expr: str, await_promise: bool = True, timeout: float = 60.0):
    """在页面里求值，返回 JSON 化后的结果。"""
    from websocket import create_connection  # 延迟导入：只有要跑 JS 时才需要

    t = page_target()
    ws = create_connection(t["webSocketDebuggerUrl"], timeout=timeout)
    try:
        msg = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        }
        ws.send(json.dumps(msg))
        while True:
            raw = ws.recv()
            data = json.loads(raw)
            if data.get("id") == 1:
                res = data.get("result", {})
                if "exceptionDetails" in res:
                    raise SystemExit(f"[js] 报错: {json.dumps(res['exceptionDetails'], ensure_ascii=False)[:400]}")
                return res.get("result", {}).get("value")
    finally:
        ws.close()


def whoami() -> dict:
    """直接打开 nav 接口页读 JSON（跨域 fetch 会被 CORS 拦，这里走导航）。"""
    nav_url = "https://api.bilibili.com/x/web-interface/nav"
    call("Page.navigate", {"url": nav_url})
    for _ in range(24):
        time.sleep(0.25)
        raw = js("document.body ? document.body.innerText : ''", await_promise=False)
        if raw and raw.strip().startswith("{"):
            d = json.loads(raw)
            data = d.get("data") or {}
            return {
                "isLogin": data.get("isLogin"),
                "uname": data.get("uname"),
                "mid": data.get("mid"),
                "level": (data.get("level_info") or {}).get("current_level"),
            }
    return {"isLogin": None, "note": "导航后没读到 JSON"}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "whoami"
    if cmd == "launch":
        launch()
        print(json.dumps(whoami(), ensure_ascii=False))
    elif cmd == "whoami":
        if not cdp_alive():
            raise SystemExit("[edge] 窗口没开，先跑 launch")
        print(json.dumps(whoami(), ensure_ascii=False))
    elif cmd == "eval":
        print(json.dumps(js(sys.argv[2]), ensure_ascii=False))
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
