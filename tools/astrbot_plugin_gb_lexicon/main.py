"""群内自学（A6）：把群里的话丢给 group-brain 复盘，让它自己学新词。

为什么要有这个插件
----------------
用户 2026-11-19 的要求：让群里的它「自己从群里学习」，碰到听不懂的话自己上网查，
而且必须在云端跑 —— 他电脑关着也得学。

这件事不能做在「回复」里：回复只有 80 字，没有地方、也没有时间（群里等 5 秒）去查一个词。
所以拆成两半：

- **这个插件（学）**：默默看着群里说话，攒够一段就 POST 给 group-brain，
  由它判词、上网、写库。群友完全感觉不到。
- **batch_reply（用）**：真正回话时先问一句「这句话里有我已经会的词吗」，
  把解释拼进 prompt。见 `astrbot_plugin_batch_reply/main.py` 里的 `_lexicon_hint`。

为什么不拦消息
------------
这个插件不需要拦、不需要回，只要「看见」。所以它不调 `stop_event`，
优先级放在 batch_reply（-200）之前（-300），先记下来，再让它去决定回不回。

成本
----
判词是一次轻量模型调用。群里一天几百条，不加限制一天能跑上千次，
所以：攒够 `min_lines` 条或过了 `interval_seconds` 才发一次、夜里不发、
每天最多 `daily_cap` 次。真正花钱的「上网查」在 group-brain 那边还有第二道限额。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import urllib.error
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot 4.x / 3.x 的导入路径不一样，两个都试
    from astrbot.api.message_components import Image, Plain  # noqa: F401
except Exception:  # noqa: BLE001
    Image = Plain = None  # type: ignore[assignment]

DEFAULT_BRAIN = "https://zkysever.com"
TOKEN_FILE = Path("/AstrBot/data/gb_lexicon_token.txt")
SKIP_PREFIX = ("/", "#", "！", "!")


class GbLexicon(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config if isinstance(config, dict) else {}
        self.enabled = bool(cfg.get("enabled", True))
        self.brain = str(cfg.get("brain_url") or DEFAULT_BRAIN).rstrip("/")
        self.token = self._token(cfg)
        self.min_lines = int(cfg.get("min_lines", 10) or 10)
        self.interval = float(cfg.get("interval_seconds", 300) or 300)
        self.daily_cap = int(cfg.get("daily_cap", 120) or 120)
        self.quiet = str(cfg.get("quiet_hours", "1-8") or "1-8")

        self._buf: dict[str, list[dict]] = {}
        self._last_flush = 0.0
        self._day = time.strftime("%Y-%m-%d")
        self._sent_today = 0
        self._task: asyncio.Task | None = None
        self._stats = {"sent": 0, "lines": 0, "learned": [], "errors": 0}

    # ------------------------------------------------------------ 配置

    def _token(self, cfg: dict) -> str:
        """token 从三个地方找：插件配置 → 环境变量 → 共享卷里的文件。

        容器里最省事的是文件：宿主写 /opt/qqbot/astrbot-data/gb_lexicon_token.txt，
        容器里就是 /AstrBot/data/gb_lexicon_token.txt，不用改 compose、不用重启配置。
        """
        t = str(cfg.get("token") or "").strip() or os.environ.get("GB_LEXICON_TOKEN", "").strip()
        if t:
            return t
        try:
            if TOKEN_FILE.exists():
                return TOKEN_FILE.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _in_quiet(self) -> bool:
        try:
            a, b = self.quiet.split("-")
            h = time.localtime().tm_hour
            a, b = int(a), int(b)
        except Exception:  # noqa: BLE001
            return False
        return a <= h < b if a <= b else (h >= a or h < b)

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._sent_today = 0

    # ------------------------------------------------------------ 记账

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-300)
    async def on_message(self, event: AstrMessageEvent):
        """只记账，不拦、不回。"""
        if not self.enabled or not self.token:
            return
        try:
            text = (event.message_str or "").strip()
        except Exception:  # noqa: BLE001
            return
        if not text or text.startswith(SKIP_PREFIX):
            return
        umo = event.unified_msg_origin
        try:
            who = event.get_sender_name() or "某人"
        except Exception:  # noqa: BLE001
            who = "某人"
        self._buf.setdefault(umo, []).append(
            {"who": who[:20], "text": text[:300], "ts": time.time()}
        )
        # 一个会话最多攒 40 条，防止长时间不 flush 时无限长
        if len(self._buf[umo]) > 40:
            del self._buf[umo][:-40]
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        if self._in_quiet():
            return
        self._roll_day()
        if self._sent_today >= self.daily_cap:
            return
        total = sum(len(v) for v in self._buf.values())
        due = total >= self.min_lines and (time.time() - self._last_flush) >= self.interval
        if not due:
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._flush_all())

    # ------------------------------------------------------------ 上报

    async def _flush_all(self) -> None:
        batch = {k: v[:20] for k, v in self._buf.items() if v}
        self._buf = {}
        self._last_flush = time.time()
        for umo, lines in batch.items():
            await self._post(umo, lines, "")

    async def _post(self, umo: str, lines: list[dict], reply: str) -> None:
        if not lines or not self.token:
            return
        payload = json.dumps(
            {
                "lines": [{"who": x.get("who"), "text": x.get("text")} for x in lines][:20],
                "reply": reply,
                "umo": umo,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        url = f"{self.brain}/api/lexicon/observe"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "X-GB-Token": self.token},
            method="POST",
        )
        try:
            # urllib 是阻塞的，扔进线程池，别卡住 AstrBot 的事件循环
            body = await asyncio.to_thread(self._do_post, req)
            data = json.loads(body or "{}")
            self._sent_today += 1
            self._stats["sent"] += 1
            self._stats["lines"] += len(lines)
            if data.get("accepted") is False:
                logger.info(f"[gb-lexicon] 服务端没接: {data.get('reason')}")
        except Exception as exc:  # noqa: BLE001
            self._stats["errors"] += 1
            logger.warning(f"[gb-lexicon] 上报失败: {type(exc).__name__}: {exc}")

    @staticmethod
    def _do_post(req: urllib.request.Request) -> str:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")

    # ------------------------------------------------------------ 收尾

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
