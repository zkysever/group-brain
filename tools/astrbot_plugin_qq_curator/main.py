"""B 站选片推送：把 group-brain 写在 outbox 里的 json 发到 QQ 私聊。

为什么是「轮询目录」而不是「监听端口」
--------------------------------------
group-brain 跑在宿主机的 systemd 里，QQ 在这个 Docker 容器里。两边唯一共享的
东西是 `/opt/qqbot/astrbot-data`（容器内 `/AstrBot/data`）。所以最省事、最不会
被防火墙/端口/重连搞坏的方式就是：一边写文件，另一边轮询它。

行为：
1. 每 60 秒扫一次 `/AstrBot/data/curator/outbox/*.json`（按文件名排序 = 按时间）；
2. 逐个 `await context.send_message(umo, MessageChain([Plain(text)]))`；
3. 成功 -> `sent/`；失败累计 3 次 -> `failed/` 并告警（别让它无限重试刷屏）。

它不发任何命令，也不会回应用户消息——纯投递器。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.core.message.components import File, Plain, Video
from astrbot.core.message.message_event_result import MessageChain

BASE = Path("/AstrBot/data/curator")
OUTBOX = BASE / "outbox"
SENT = BASE / "sent"
FAILED = BASE / "failed"

INTERVAL = 60          # 秒
MAX_TRY = 3            # 同一个文件最多试几次


class QQCurator(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        self.umo = str(self.config.get("umo") or "qqbot:FriendMessage:3750575880")
        self.enabled = bool(self.config.get("enabled", True))
        self._task: asyncio.Task | None = None
        self._tries: dict[str, int] = {}

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        for d in (OUTBOX, SENT, FAILED):
            d.mkdir(parents=True, exist_ok=True)
        if not self.enabled:
            logger.info("[curator] 已在配置里关掉，不启动轮询")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
        logger.info(f"[curator] 开始轮询 {OUTBOX} -> {self.umo}")

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    # ------------------------------------------------------------ 主循环

    async def _loop(self) -> None:
        # 等 AstrBot 把平台适配器都拉起再开始，否则第一轮必失败
        await asyncio.sleep(15)
        while True:
            try:
                await self._drain()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[curator] 轮询出错: {type(exc).__name__}: {exc}")
            await asyncio.sleep(INTERVAL)

    async def _drain(self) -> None:
        files = sorted(OUTBOX.glob("*.json"))
        if not files:
            return
        for path in files:
            key = path.name
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[curator] {key} 不是合法 json，移到 failed: {exc}")
                self._move(path, FAILED)
                continue

            text = (payload.get("text") or "").strip()
            if not text:
                self._move(path, FAILED)
                continue

            ok = await self._send(text, payload.get("file"), payload.get("name"),
                                  payload.get("video_url"))
            if ok:
                self._move(path, SENT)
                logger.info(f"[curator] 已推送 {payload.get('bvid') or key} 到 {self.umo}")
                self._tries.pop(key, None)
            else:
                n = self._tries.get(key, 0) + 1
                self._tries[key] = n
                logger.warning(f"[curator] {key} 第 {n} 次发送失败")
                if n >= MAX_TRY:
                    self._move(path, FAILED)
                    self._tries.pop(key, None)

    async def _send(self, text: str, file_path: str | None = None,
                    name: str | None = None, video_url: str | None = None) -> bool:
        try:
            if video_url:
                # 视频必须是「一条消息里唯一的一段」，所以先发视频，再发文字
                ok = await self.context.send_message(
                    self.umo, MessageChain([Video.fromURL(video_url)]))
                if not ok:
                    return False
                return bool(await self.context.send_message(
                    self.umo, MessageChain([Plain(text)])))

            chain = [Plain(text)]
            if file_path:
                # 图片/文件走文件消息：File(name=..., file=容器内路径)
                chain.append(File(name or Path(file_path).name, file=file_path))
            ret = await self.context.send_message(self.umo, MessageChain(chain))
            return bool(ret)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[curator] send_message 抛错: {type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _move(path: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = dest / f"{stamp}-{path.name}"
        try:
            shutil.move(str(path), str(target))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[curator] 移动 {path.name} 失败: {exc}")
