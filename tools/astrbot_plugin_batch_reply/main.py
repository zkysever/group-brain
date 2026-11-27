"""等 5 秒再回：把一阵消息合并成一次回复。

为什么要这个
-----------
群里聊天是一阵一阵的：一句话被拆成三四条、中间还夹着表情包。原来的做法是
来一条回一条，读起来像插嘴。这个插件改成：

1. 每条消息只**记账**（谁说的、说了什么、带图没有），不立刻回；
2. 每条新消息把计时器重置为 `idle_seconds`（默认 5 秒）；
3. 计时器真的走完了（也就是 5 秒内没人再说话），才把这一阵话一次性交给模型，
   只回一条。

触发条件
--------
- 私聊：任何消息都算「在跟我说话」。
- 群聊：默认只有被 @ 到时才算（`group_reply_needs_at`）。没被 @ 的群消息也会进
  缓冲区当上下文，但不触发回复——这样它知道前面在聊什么，又不会自己插话。

表情包
------
按 `meme_probability` 随机配一张，和文字放在同一条消息里发出去，不单独解说。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

DEFAULT_PACK = Path(
    "/AstrBot/data/plugin_data/meme_manager/packs/deepseek-fanart-meme-pack/memes"
)
IMAGE_EXTS = {".webp", ".jpg", ".jpeg", ".png", ".gif"}

# ---------------------------------------------------------------- group-brain（A6 群内自学）
# 容器里连不上宿主的 127.0.0.1:8000，走隧道域名（HTTPS，token 校验）。
# 拿不到就退回纯聊天：这两个调用都是「锦上添花」，挂了不许影响回复。
GB_BRAIN = (os.environ.get("GB_BRAIN_URL") or "https://zkysever.com").rstrip("/")
GB_TOKEN_FILE = Path("/AstrBot/data/gb_lexicon_token.txt")
GB_TIMEOUT_GET = 1.5   # 查词库在路上；回复不该等一本词典超过 1.5 秒
GB_TIMEOUT_POST = 15.0  # 复盘是后台任务，可以慢
# Cloudflare 会挡掉 urllib 的默认 UA（403 error code 1010），伪装成浏览器
CLOUDFLARE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _gb_token() -> str:
    t = os.environ.get("GB_LEXICON_TOKEN", "").strip()
    if t:
        return t
    try:
        if GB_TOKEN_FILE.exists():
            return GB_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _gb_call(method: str, path: str, params: dict | None = None,
             payload: dict | None = None, timeout: float = GB_TIMEOUT_GET) -> dict:
    token = _gb_token()
    if not token:
        return {}
    url = GB_BRAIN + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"X-GB-Token": token, "User-Agent": CLOUDFLARE_UA}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")

SYSTEM = """你是「蓝色大肥鱼」，一个住在 QQ 群里的 AI 助手，性别不明、说话随意、有点贫但热心。

规则：
1. 你看到的是这一阵子里连着发的几条消息，读完整再答，不要一条一条对。
2. 回得短：一般一到两句，不超过 80 字。别写小作文，别分点，别用「首先/其次」。
3. 群里有人互相开玩笑时，你可以接一句；但不要评价谁对谁错，不要劝架。
4. 不知道就说不知道，不要编。别人问群里的事，你不知道就说「这我真不知道」。
5. 不要自称「AI 语言模型」「助手」这类词，你就是群里那只鱼。
"""


class BatchReply(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config if isinstance(config, dict) else {}
        self.cfg = cfg
        self.idle = float(cfg.get("idle_seconds", 5) or 5)
        self.need_at = bool(cfg.get("group_reply_needs_at", True))
        self.meme_p = float(cfg.get("meme_probability", 0.35) or 0)
        self.max_buf = int(cfg.get("max_buffer", 20) or 20)
        self.max_chars = int(cfg.get("max_reply_chars", 80) or 80)
        self.enabled = bool(cfg.get("enabled", True))
        # 群内词库（A6）：开着才在回复里用、才上报复盘；关掉就是纯聊天。
        self.lexicon = bool(cfg.get("lexicon", True))
        # 看图（2026-11-25 加）：回复用的 DeepSeek 是纯文本模型，图在它眼里只有一个
        # 「[图片]」字样。所以先让一个视觉模型把图说成人话，再塞进 prompt。
        self.caption_provider_id = str(
            cfg.get("caption_provider") or "dashscope/qwen3-vl-plus"
        )
        self.caption_max = int(cfg.get("caption_max", 2) or 0)
        self._cap_cache: dict[str, str] = {}
        pack = str(cfg.get("meme_pack") or DEFAULT_PACK)
        self.pack = Path(pack)

        self._buf: dict[str, deque] = {}
        self._task: dict[str, asyncio.Task] = {}
        self._last_meme: dict[str, str] = {}

    # ---------------------------------------------------------------- 记账

    @filter.event_message_type(
        filter.EventMessageType.ALL, priority=-200
    )
    async def on_message(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        umo = event.unified_msg_origin
        text = (event.message_str or "").strip()

        # 自己发的、以及空消息（纯图片且没文字）也记，但标记带图
        has_image = bool(event.get_messages() and any(
            isinstance(seg, Image) for seg in event.get_messages()))
        if not text and not has_image:
            return
        if not text:
            text = "[图片]"

        is_group = ":GroupMessage:" in umo
        at_me = bool(event.is_at_or_wake_command)
        trigger = (not is_group) or (at_me if self.need_at else True)

        # 命令（/ 开头）放行，交给 AstrBot 自己处理，不进缓冲区也不拦
        if text.startswith("/"):
            return

        imgs = [seg for seg in (event.get_messages() or []) if isinstance(seg, Image)]
        buf = self._buf.setdefault(umo, deque(maxlen=self.max_buf))
        buf.append({"who": event.get_sender_name() or "某人", "text": text,
                    "imgs": imgs, "at": at_me, "ts": time.time()})
        # 触发标记单独占一格（不是消息），所以文本里不留空行
        if trigger:
            self._buf[umo].append({"trigger": True})  # 只做标记，触发用

        # 重置计时
        old = self._task.get(umo)
        if old and not old.done():
            old.cancel()
        self._task[umo] = asyncio.create_task(self._fire_later(umo))

        # 一律拦下：群里没被 @ 时也不许别的环节自己去回，否则会出现
        # 「一条消息回两遍」和「逐条点评表情包」。回不回由这里统一决定。
        event.stop_event()

    # ---------------------------------------------------------------- 计时

    async def _fire_later(self, umo: str) -> None:
        try:
            await asyncio.sleep(self.idle)
        except asyncio.CancelledError:
            return
        try:
            await self._flush(umo)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[batch] 汇总回复失败: {type(exc).__name__}: {exc}")

    async def _flush(self, umo: str) -> None:
        buf = self._buf.pop(umo, None)
        if not buf:
            return
        lines = [x for x in buf if x.get("text")]
        trigger = any(x.get("trigger") for x in buf)
        if not trigger:
            return                      # 没叫我，只是攒着当上下文，静默清掉
        if not lines:
            return

        recent = await self._with_images(lines[-self.max_buf:])
        convo = "\n".join(f"{x['who']}：{x['text']}" for x in recent)
        prompt = f"这是刚才群里连着发的消息（最后一条是在叫我）：\n{convo}\n\n回一句。"
        prompt = await self._enrich(prompt, convo)
        reply = await self._ask(umo, prompt)
        if not reply:
            return
        await self._send(umo, reply)
        # 复盘刚才这段：让它判词、上网查、记住（后台，不挡回复）
        asyncio.create_task(self._remember(lines, reply))

    # ---------------------------------------------------------------- 看图

    async def _with_images(self, lines: list[dict]) -> list[dict]:
        """把每张图说成人话，拼回那一行的文本后面。

        为什么要这一步：回复模型是 DeepSeek（纯文本），图在 prompt 里只有一个
        「[图片]」。玩家发表情包、发截图，它一律说「图没加载出来」。
        所以先用视觉模型（默认 dashscope/qwen3-vl-plus）描述一遍。

        成本控制：
        - 一次最多描述 `caption_max` 张（默认 2）；
        - 同一个 URL / 文件用过的描述存内存，重复发不重复花钱；
        - 任何一步失败都退回原来的「[图片]」，不影响回复。
        """
        if self.caption_max <= 0 or not lines:
            return lines

        got = 0
        out: list[dict] = []
        for x in lines:
            imgs = x.get("imgs") or []
            if not imgs or got >= self.caption_max:
                out.append(x)
                continue
            cap = await self._caption(imgs[0])
            got += 1
            if not cap:
                out.append(x)
                continue
            base = (x.get("text") or "").replace("[图片]", "").strip()
            text = f"{base}（图：{cap}）" if base else f"（发来一张图：{cap}）"
            out.append({**x, "text": text})
        return out

    async def _caption(self, seg) -> str:
        key = (seg.url or seg.file or "")[:400]
        if key and key in self._cap_cache:
            return self._cap_cache[key]
        try:
            prov = self.context.get_provider_by_id(self.caption_provider_id)
        except Exception:  # noqa: BLE001
            prov = None
        if prov is None:
            logger.info(f"[batch] 没有视觉 provider: {self.caption_provider_id}")
            return ""
        try:
            raw = await seg.convert_to_base64()
            url = "data:image/png;base64," + raw
        except Exception:  # noqa: BLE001
            url = seg.url or seg.file or ""
        if not url:
            return ""
        try:
            resp = await prov.text_chat(
                prompt="用中文一句话说清这张图里是什么；如果是表情包或梗图，"
                       "补一句它想表达的情绪。不超过 40 字。",
                session_id=uuid.uuid4().hex,
                image_urls=[url],
                persist=False,
            )
            cap = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001 —— 看图挂了就当没有图
            logger.info(f"[batch] 看图失败: {type(exc).__name__}: {str(exc)[:80]}")
            return ""
        if cap:
            if len(key) and len(self._cap_cache) < 300:
                self._cap_cache[key] = cap
            logger.info(f"[batch] 看图: {cap[:40]}")
        return cap

    # ---------------------------------------------------------------- 学

    async def _enrich(self, prompt: str, convo: str) -> str:
        """把「这句话里它已经会的词」拼进 prompt。

        为什么在这里查、而不是让它自己调工具：QQ 回复只有一次模型调用，
        没有工具循环；而且它得「先知道自己不知道」才会查，那恰恰是不会发生的事。
        """
        if not self.lexicon:
            return prompt
        try:
            data = await asyncio.to_thread(
                _gb_call, "GET", "/api/lexicon/context", {"text": convo[-400:]}, None
            )
        except Exception as exc:  # noqa: BLE001 —— 词库挂了照常聊天
            logger.info(f"[batch] 词库查询跳过: {type(exc).__name__}")
            return prompt
        glosses = data.get("glosses") or []
        if not glosses:
            return prompt
        tip = "；".join(f"{g.get('term')}＝{g.get('gloss')}" for g in glosses[:3])
        return prompt + f"\n\n（群内词库，可以直接用，不用解释来源：{tip}）"

    async def _remember(self, lines: list[dict], reply: str) -> None:
        """把这一阵群聊 + 自己的回复发给 group-brain 复盘。失败静默。"""
        if not self.lexicon:
            return
        try:
            await asyncio.to_thread(
                _gb_call,
                "POST",
                "/api/lexicon/observe",
                None,
                {
                    "lines": [{"who": x["who"], "text": x["text"]} for x in lines][-20:],
                    "reply": reply[:300],
                },
                GB_TIMEOUT_POST,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[batch] 复盘上报跳过: {type(exc).__name__}")

    # ---------------------------------------------------------------- 模型

    async def _ask(self, umo: str, prompt: str) -> str:
        prov = self.context.get_using_provider(umo)
        if prov is None:
            logger.warning("[batch] 没有可用的模型 provider")
            return ""
        resp = await prov.text_chat(prompt=prompt, system_prompt=SYSTEM)
        text = (getattr(resp, "completion_text", "") or "").strip()
        if len(text) > self.max_chars * 3:
            text = text[: self.max_chars * 3]
        return text

    # ---------------------------------------------------------------- 发送

    def _pick_meme(self, umo: str) -> Path | None:
        if self.meme_p <= 0 or random.random() > self.meme_p:
            return None
        if not self.pack.exists():
            return None
        files = [p for p in self.pack.rglob("*") if p.suffix.lower() in IMAGE_EXTS]
        if not files:
            return None
        last = self._last_meme.get(umo)
        for _ in range(6):
            p = random.choice(files)
            if str(p) != last:
                self._last_meme[umo] = str(p)
                return p
        return random.choice(files)

    async def _send(self, umo: str, text: str) -> None:
        chain = [Plain(text)]
        meme = self._pick_meme(umo)
        if meme is not None:
            try:
                chain.append(Image.fromFileSystem(str(meme)))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[batch] 表情包挂不上去: {exc}")
        ok = await self.context.send_message(umo, MessageChain(chain))
        logger.info(f"[batch] 回复 {umo}: {text[:30]}{'…' if len(text) > 30 else ''} "
                    f"{'(带图)' if meme else ''} ok={bool(ok)}")

    async def terminate(self) -> None:
        for t in self._task.values():
            if not t.done():
                t.cancel()
