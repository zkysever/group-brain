"""本机网页阅读服务：把 bsk 驱动的真浏览器借给 zkysever.com 的服务器用。

为什么需要它：group-brain 跑在阿里云，抓不到「403 / 要跑 JS」的页面；
这台 Windows 上的 Chromium 有你自己的登录态，能打开它们。
服务器通过 SSH 反向隧道 `ssh -N -R 8010:127.0.0.1:8010 gb` 访问本服务。

只监听 127.0.0.1，不对外暴露。页面上的一切文字都当数据用，不当指令。

实测结论（2026-11-16）：
- `bsk open <url>` 必须带 `--observation <bsk sync --json 的 observation_id>`，否则
  报 `this action needs an explicit current observation`；偶发失败时重试一次即可。
- `bsk observe` 不带 `--tab-id` 有时返回空文本，**必须显式带上标签页 id**。
- `bsk read` 不可用（浏览器扩展协议 1.8 < daemon 1.9），所以自己从 VOM 里抽文字。
"""

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BSK = os.environ.get("BSK_EXE") or r"C:\Users\Administrator\.local\bin\bsk.exe"
PORT = int(os.environ.get("BSK_READER_PORT", "8010"))
WAIT_SECONDS = float(os.environ.get("BSK_READER_WAIT", "12"))
MIN_TEXT = int(os.environ.get("BSK_READER_MIN_TEXT", "60"))
MAX_TEXT = int(os.environ.get("BSK_READER_MAX_TEXT", "6000"))
TOKEN = os.environ.get("BSK_READER_TOKEN", "").strip()

_LOCK = threading.Lock()          # 一个浏览器，串行用
_ENV = {**os.environ, "BSK_AUTO_START": "0"}
_RETRY_HINT = ("needs an explicit current observation", "control context")


def _run(args, timeout=30.0):
    return subprocess.run(
        [BSK, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_ENV,
        timeout=timeout,
    )


def _json(args, timeout=30.0):
    p = _run(args, timeout)
    out = (p.stdout or "").strip()
    if not out:
        raise RuntimeError((p.stderr or "bsk 没有输出").strip()[:300])
    return json.loads(out)


def _state():
    return _json(["sync", "--json"])


def _needs_retry(text):
    return any(h in (text or "") for h in _RETRY_HINT)


def daemon_ok():
    try:
        p = _run(["status"], timeout=15)
        txt = (p.stdout or "") + (p.stderr or "")
        tail = txt.split("browsers connected")[-1][:8] if "browsers connected" in txt else ""
        return "1" in tail
    except Exception:
        return False


def _clean(vom):
    """从 VOM 里抽出人能读的文字：取所有引号里的可访问名，去掉相邻重复。"""
    parts = [s.strip() for s in re.findall(r'"([^"]{2,})"', vom or "")]
    out, prev = [], None
    for s in parts:
        if s == prev:
            continue
        out.append(s)
        prev = s
    return "\n".join(out)[:MAX_TEXT]


def _navigate(url):
    """导航到 url，返回 tab_id。失败时按 bsk 的提示重试一次。"""
    last = ""
    for attempt in (1, 2):
        st = _state()
        obs, tab = st.get("observation_id"), st.get("tab_id")
        p = _run(["open", url, "--observation", obs])
        if p.returncode == 0:
            return tab
        last = ((p.stderr or "") + (p.stdout or "")).strip()
        if attempt == 1 and _needs_retry(last):
            time.sleep(1.0)
            continue
        break
    raise RuntimeError(last.splitlines()[0][:300] if last else "打不开这个网址")


def _read_text(tab):
    """轮询拿正文：优先 observe（带 tab-id），再退回 sync 的大块 VOM。"""
    deadline = time.time() + WAIT_SECONDS
    best = ""
    while time.time() < deadline:
        time.sleep(0.7)
        try:
            obs = _json(["observe", "--json", "--tab-id", str(tab)])
            text = _clean(obs.get("text") or "")
        except Exception:
            text = ""
        if not text:
            try:
                text = _clean(_state().get("vom") or "")
            except Exception:
                text = ""
        if len(text) > len(best):
            best = text
        if len(best) >= MIN_TEXT * 3:
            break
    return best


def read(url):
    if not re.match(r"^https?://", url or ""):
        raise ValueError("只接受 http/https")
    with _LOCK:
        tab = _navigate(url)
        text = _read_text(tab)
        if len(text) < MIN_TEXT:
            raise RuntimeError(f"页面几乎没有可读文字（提取到 {len(text)} 字）")
        return {"ok": True, "url": url, "tab": tab, "chars": len(text), "text": text}


class Handler(BaseHTTPRequestHandler):
    server_version = "bsk-reader/0.2"

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, {"ok": True, "daemon": daemon_ok(), "port": PORT})
        if u.path != "/read":
            return self._send(404, {"ok": False, "error": "只有 /read 和 /health"})
        if TOKEN and self.headers.get("x-bsk-token", "") != TOKEN:
            return self._send(403, {"ok": False, "error": "token 不对"})
        url = (parse_qs(u.query).get("url") or [""])[0]
        started = time.time()
        try:
            body = read(url)
        except Exception as exc:
            body = {"ok": False, "url": url, "error": f"{type(exc).__name__}: {exc}"}
        body["ms"] = int((time.time() - started) * 1000)
        return self._send(200, body)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args), flush=True)


if __name__ == "__main__":
    print(f"bsk-reader 监听 127.0.0.1:{PORT}（bsk: {BSK}）", flush=True)
    print(f"daemon 在线: {daemon_ok()}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
