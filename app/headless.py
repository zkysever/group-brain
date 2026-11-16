"""服务器自己的无头浏览器。

为什么要有这一层：`websearch.fetch` 早期只有「纯 HTTP」和「借主人电脑上的真浏览器」
两条路。后者依赖他开着机、开着 bsk 的 Agent Window，电脑一关就退化成抓不到；
前者读不了 JS 渲染的页面（知乎、掘金、SPA 都是空壳）。

这一层把「开网页」这件事搬回服务器自己：Playwright + chromium-headless-shell，
进程内串行（同一时刻只开一个页面），资源只放行 html/css，图片视频字体一律 abort，
单次超时 25 秒。抓不到就返回 None，由上层决定降级——**绝不让浏览器的故障
变成一个 500**。

内存：headless shell 单页约 250–400MB，服务器 1.7G 内存 + 4G swap，
所以并发必须是 1（`_LOCK`），并且每次用完 `browser.close()`。
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

# 浏览器二进制装在仓库里的固定目录，不依赖调用方的环境变量
# （systemd 里由 drop-in 传，但 cron、ssh 手测、脚本调用都没有，容易踩「找不到浏览器」）
_LOCAL_BROWSERS = "/opt/group-brain/.pw-browsers"
if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and os.path.isdir(_LOCAL_BROWSERS):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _LOCAL_BROWSERS

MAX_CHARS = int(os.environ.get("HEADLESS_MAX_CHARS", "4000"))
TIMEOUT = float(os.environ.get("HEADLESS_TIMEOUT", "25"))
MIN_TEXT = int(os.environ.get("HEADLESS_MIN_TEXT", "120"))
# 服务器上没有 GPU、/dev/shm 很小，这几个参数是省内存的关键
ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--single-process",
    "--no-zygote",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
    "--hide-scrollbars",
]
BLOCK = {"image", "media", "font", "stylesheet"}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"ok": None, "at": 0.0, "err": ""}


def enabled() -> bool:
    return os.environ.get("HEADLESS", "1") != "0"


def _import_ok() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        _STATE.update({"ok": False, "err": f"no playwright: {type(exc).__name__}: {exc}"})
        return False
    return True


def available(warm: bool = False) -> bool:
    """能不能用。默认只看「模块在不在」，不真的起浏览器（起一次要 1 秒）。"""
    if not enabled():
        return False
    now = time.time()
    if _STATE["ok"] is not None and now - _STATE["at"] < 60:
        return bool(_STATE["ok"])
    ok = _import_ok()
    _STATE.update({"ok": ok, "at": now})
    return ok


def _clean(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if len(ln) >= 2]
    return "\n".join(lines).strip()


def fetch(url: str, max_chars: int = MAX_CHARS, timeout: float = TIMEOUT) -> dict[str, str] | None:
    """用服务器自己的 chrome 打开一个页面，返回可读文本。失败返回 None（不抛）。"""
    if not available():
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        return None

    started = time.time()
    with _LOCK:  # 同一时刻只开一个浏览器
        browser = None
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=ARGS)
                ctx = browser.new_context(
                    user_agent=UA,
                    locale="zh-CN",
                    viewport={"width": 1280, "height": 900},
                    java_script_enabled=True,
                )
                page = ctx.new_page()
                page.route("**/*", lambda route: route.abort()
                           if route.request.resource_type in BLOCK else route.continue_())
                page.set_default_timeout(timeout * 1000)
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:  # noqa: BLE001  有些站永远不 idle，不等了
                    page.wait_for_timeout(800)
                title = (page.title() or "").strip()
                try:
                    text = page.inner_text("body")
                except Exception:  # noqa: BLE001
                    text = page.content()
                text = _clean(text)[:max_chars]
                ms = int((time.time() - started) * 1000)
                if len(text) < MIN_TEXT:
                    print(f"[warn] headless 只拿到 {len(text)} 字: {url}", flush=True)
                    return None
                return {"title": title[:120], "url": url, "text": text, "via": "headless", "ms": ms}
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] headless 失败 {url}: {type(exc).__name__}: {exc}", flush=True)
            return None
        finally:
            try:
                if browser is not None:
                    browser.close()
            except Exception:  # noqa: BLE001
                pass


def stats() -> dict[str, Any]:
    return {"enabled": enabled(), "ok": available(), "err": _STATE.get("err", "")}
