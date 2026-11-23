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


_TARGET = {"id": None}


class Conn:
    """一条长连接上的 CDP 会话。

    为什么要有它：DOM 域的状态（文档、nodeId）是**按连接**记的。每次 call() 都新开
    一条 websocket 的话，`DOM.getDocument` 拿到的 nodeId 在下一次调用里就失效
    （`DOM.requestNode` 直接返回 null），文件选择这类多步操作根本做不成。
    """

    def __init__(self, timeout: float = 60.0):
        from websocket import create_connection

        self.ws = create_connection(page_target()["webSocketDebuggerUrl"], timeout=timeout)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            try:
                data = json.loads(self.ws.recv())
            except Exception:
                return None
            if data.get("id") == mid:
                if data.get("error"):
                    return {"__error__": data["error"]}
                return data.get("result")

    def js(self, expr: str, await_promise: bool = True):
        res = self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": await_promise, "userGesture": True},
        ) or {}
        if "__error__" in res:
            raise SystemExit(f"[js] 报错: {json.dumps(res['__error__'], ensure_ascii=False)[:300]}")
        if "exceptionDetails" in res:
            raise SystemExit(f"[js] 报错: {json.dumps(res['exceptionDetails'], ensure_ascii=False)[:300]}")
        return (res.get("result") or {}).get("value")

    def obj_id(self, expr: str) -> str | None:
        res = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": False}) or {}
        return (res.get("result") or {}).get("objectId")

    def set_file(self, selector: str, path: str) -> dict:
        """把本地文件塞进 <input type=file>（必须在同一条连接里做完）。"""
        self.call("DOM.enable", {})
        self.call("DOM.getDocument", {"depth": -1})
        obj = self.obj_id(f"document.querySelector({json.dumps(selector)})")
        if not obj:
            return {"ok": False, "why": "selector 没命中"}
        node = self.call("DOM.requestNode", {"objectId": obj}) or {}
        nid = node.get("nodeId")
        if not nid:
            return {"ok": False, "why": "requestNode 没拿到 nodeId", "node": node}
        r = self.call("DOM.setFileInputFiles", {"nodeId": nid, "files": [path]})
        if r is None or (isinstance(r, dict) and "__error__" in r):
            return {"ok": False, "why": "setFileInputFiles 失败", "res": r}
        return {"ok": True, "node": nid}

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def session(timeout: float = 60.0) -> Conn:
    return Conn(timeout=timeout)


def _browser_ws() -> str:
    return _http("/json/version").get("webSocketDebuggerUrl") or ""


def _browser_call(method: str, params: dict, timeout: float = 20.0):
    from websocket import create_connection

    ws = create_connection(_browser_ws(), timeout=timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == 1:
                return data.get("result")
    finally:
        ws.close()


def new_tab(url: str, wait: float = 7.0) -> str | None:
    """开一个干净标签页并把它设为默认目标。

    为什么要新开：编辑页开过封面编辑器之后，同一个标签页常常整页卡死
    （Runtime.evaluate 全部超时、Page.reload 也没反应），换新页最省事。
    """
    res = _browser_call("Target.createTarget", {"url": url}) or {}
    tid = res.get("targetId")
    _TARGET["id"] = tid
    time.sleep(wait)
    return tid


def close_tab() -> None:
    tid = _TARGET["id"]
    if not tid:
        return
    try:
        _browser_call("Target.closeTarget", {"targetId": tid})
    except Exception:
        pass
    _TARGET["id"] = None


def page_target() -> dict:
    """挑一个可用的 page 目标：先认 new_tab 指定的，再优先 bilibili 的。"""
    tabs = [t for t in _http("/json/list") if t.get("type") == "page"]
    if not tabs:
        raise SystemExit("[edge] 没有可用的标签页")
    if _TARGET["id"]:
        for t in tabs:
            if t.get("id") == _TARGET["id"]:
                return t
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
        try:
            while True:
                data = json.loads(ws.recv())
                if data.get("id") == 1:
                    return data.get("result")
        except Exception:
            return None  # 导航中目标会换掉，回执丢了很正常
    finally:
        ws.close()


def js(expr: str, await_promise: bool = True, timeout: float = 25.0):
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
            try:
                raw = ws.recv()
            except Exception:
                return None  # 页面跳转/关闭时回执会丢，别抛
            data = json.loads(raw)
            if data.get("id") == 1:
                res = data.get("result", {})
                if "exceptionDetails" in res:
                    raise SystemExit(f"[js] 报错: {json.dumps(res['exceptionDetails'], ensure_ascii=False)[:400]}")
                return res.get("result", {}).get("value")
    finally:
        ws.close()


def nav(url: str, wait: float = 5.0) -> None:
    """用页面自己的 location 跳转（Page.navigate 在跨站导航时常常收不到回执）。"""
    call("Runtime.evaluate", {"expression": f"location.href = {json.dumps(url)}", "returnByValue": True})
    time.sleep(wait)


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
