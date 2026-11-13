"""多查询扩展（v0.17）：把「问法」翻译成「可能的写法」。

为什么需要它：难评测集 50 条里有 24 条（48%）和期望答案是**零字面重叠**的。
    「有人让我们二选一挑饮料」  → 群里原话是「百事还是可口」
    「它拒绝做扒人隐私的事了吗」 → 群里原话是「开盒这事儿我不干」
关键词那一路对这类问题天生 0 命中（一个二元组都不共享），再好的 bm25 也救不了。
所以让模型把问题改写成几种「群友真会打出来的话」，再拿这些说法去跑**关键词**那一路。

刻意只给关键词那一路用扩展，不给向量那一路用：
  - 向量那一路每加一个问题就要多调一次 embedding 接口（约 +300ms、要钱、要联网），
    而扩展出来的词本来就是为了「字面命中」服务的；
  - 关键词那一路是纯本地的，多跑几次几乎不花钱。
换句话说：用最贵的东西换最不必要的收益，不做。

成本：每个**没缓存过**的问题一次 DeepSeek 调用（约 1 秒、几分钱），
结果落在 query_expansions 表里，同一句话之后都命中缓存。
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any

from . import config as cfg

MODEL = cfg.DEEPSEEK_MODEL
URL = cfg.DEEPSEEK_URL

_SYSTEM = (
    "你在帮一个 QQ 群聊检索系统做「查询扩展」。"
    "用户输入一句话，你要给出这句话的其它说法，用来在聊天记录里按字面找到原文。"
)

_PROMPT = """原句：{q}

要求：
1. 输出 3 条，每条不超过 12 个字，是群友真会打出来的话或关键词；
2. 尽量换用不同的词（同义说法、口语、可能出现在记录里的原词），不要只是删字；
3. 不要解释、不要编号、不要 markdown，只输出一个 JSON 数组，例如 ["甲","乙","丙"]"""

# 第二版提示词（2026-11-13 晚加的）：不猜「这句话还能怎么说」，而是猜「记录里那句话长什么样」。
# 第一版问句改写在难集上是负收益（0.760 → 0.720），因为模型给的是同义问句，
# 而难集缺的字在**答案那一侧**（问「鱼什么时候才开口说话」，记录里是「不@就不会说话」）。
_PROMPT_RECORD = """用户的问法：{q}

你现在不是回答问题，是要猜「群里当时那句话长什么样」。
要求：
1. 输出 3 条，每条不超过 12 字，写成群友在群里真会发的陈述句片段，
   不要问句、不要出现「谁 / 什么时候 / 为什么 / 吗 / 怎么」；
2. 优先用具体的口语词和动作词（例：问「它什么时候才说话」→ 可能是「不@就不说话」「怎么不理人」）；
3. 不要解释、不要编号，只输出一个 JSON 数组，例如 ["甲","乙","丙"]"""

_PROMPTS = {"generic": _PROMPT, "record": _PROMPT_RECORD}

_SPLIT = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


class ExpandError(RuntimeError):
    """扩展接口出错。和 embedding 一样：带上服务端原话，否则排查只能靠猜。"""


def available() -> bool:
    return bool(cfg.DEEPSEEK_KEY)


def norm_key(text: str) -> str:
    """缓存键：NFKC + 小写 + 去掉标点空白。和 learn.norm 一个思路，但这里不依赖它。"""
    t = unicodedata.normalize("NFKC", text or "").lower()
    return _SPLIT.sub("", t)


def _post(payload: dict, timeout: int = 30, attempts: int = 2) -> dict:
    if not available():
        raise ExpandError("没配 DEEPSEEK_API_KEY，查询扩展不可用")
    body = json.dumps(payload).encode("utf-8")
    last: Exception | None = None
    for i in range(attempts):
        req = urllib.request.Request(
            URL,
            data=body,
            headers={
                "Authorization": f"Bearer {cfg.DEEPSEEK_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and i < attempts - 1:
                last = e
                time.sleep(1.2 * (i + 1))
                continue
            raise ExpandError(f"扩展接口 {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if i < attempts - 1:
                time.sleep(1.2 * (i + 1))
                continue
            raise ExpandError(f"扩展接口连不上: {type(e).__name__}: {e}") from e
    raise ExpandError(f"扩展接口重试 {attempts} 次仍失败: {last}")


def _parse(raw: str, question: str) -> list[str]:
    """从模型输出里抠出数组。模型偶尔会加解释或 markdown 围栏，所以用正则找 [...]。"""
    m = re.search(r"\[.*?\]", raw or "", re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []

    qkey = norm_key(question)
    out: list[str] = []
    for item in arr:
        if not isinstance(item, str):
            continue
        s = item.strip().strip("，。、\"'").strip()
        if not s or len(s) > 24:
            continue
        if norm_key(s) == qkey:          # 和原句一样就没意义
            continue
        if s not in out:
            out.append(s)
    return out[: cfg.MQ_MAX]


def _from_cache(conn: Any, qkey: str) -> list[str] | None:
    try:
        row = conn.execute(
            "SELECT alts FROM query_expansions WHERE qkey = ?", (qkey,)
        ).fetchone()
    except Exception:  # noqa: BLE001  表还没建（老库）时别让搜索崩掉
        return None
    if not row:
        return None
    try:
        arr = json.loads(row["alts"])
        return [x for x in arr if isinstance(x, str)][: cfg.MQ_MAX]
    except Exception:  # noqa: BLE001
        return None


def _to_cache(conn: Any, qkey: str, alts: list[str]) -> None:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO query_expansions (qkey, alts, ts) VALUES (?, ?, ?)",
            (qkey, json.dumps(alts, ensure_ascii=False), time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    except Exception:  # noqa: BLE001  缓存写不进去不该影响搜索
        pass


def expand(conn: Any, question: str, use_cache: bool = True) -> list[str]:
    """返回这句话的其它说法（不含原句）。失败抛 ExpandError，由上层决定退不退回原句。"""
    q = (question or "").strip()
    style = getattr(cfg, "MQ_PROMPT", "generic")
    prompt = _PROMPTS.get(style, _PROMPT)
    # 缓存键带风格：换提示词以后不能复用旧结果，否则量出来的还是老版本的数
    qkey = f"{style}:{norm_key(q)}"
    if len(norm_key(q)) < 2:
        return []
    if use_cache:
        cached = _from_cache(conn, qkey)
        if cached is not None:
            return cached

    data = _post(
        {
            "model": MODEL,
            "temperature": 0.3,
            "max_tokens": 120,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt.format(q=q)},
            ],
        }
    )
    try:
        raw = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ExpandError(f"扩展接口返回格式不对: {str(data)[:200]}") from e

    alts = _parse(raw, q)
    if use_cache:
        _to_cache(conn, qkey, alts)
    return alts


def stats(conn: Any) -> dict[str, Any]:
    def one(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()[0] or 0)
        except Exception:  # noqa: BLE001
            return 0

    return {
        "on": bool(cfg.MULTI_QUERY),
        "cached": one("SELECT COUNT(*) FROM query_expansions"),
        "available": available(),
    }
