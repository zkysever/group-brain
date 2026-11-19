"""全局路径与配置。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
# GB_DB 可以让数据库换个位置：CI 上跑自检用临时库（不碰真数据），
# 平时不设这个变量，就是 data/group_brain.db。
DB_PATH = Path(os.environ.get("GB_DB") or (DATA_DIR / "group_brain.db"))
WEB_DIR = ROOT / "web"
EVAL_DIR = ROOT / "eval"

# AstrBot 数据库副本（三件套一起拷过来才能读到 WAL 里的内容）
ASTRBOT_DB = RAW_DIR / "data_v4.db"

# 表情包图库：本机 .dsh/meme-packs 下的每套 pack 都有 index.db + memes/
MEME_PACKS_DIR = Path(
    os.environ.get("MEME_PACKS", str(Path.home() / ".dsh" / "meme-packs"))
)

HOST = "127.0.0.1"
PORT = 8000

# 可选：配置了硅基流动 key 才启用向量检索。
# 优先读环境变量（服务器上放 .env），没有就读本机 secrets 文件（自己跑的时候）。
SECRETS_DIR = Path.home() / ".dsh" / "secrets"


def _read_secret(env_name: str, file_name: str) -> str:
    """优先环境变量，其次 ~/.dsh/secrets/<file_name>（本机自己跑的时候）。

    secrets 文件是人类手写的，里面常带注释（`# 余额 98.05 元`）甚至整段说明，
    所以不能直接把整个文件当 key —— 那样会得到
    `InvalidHeader: ... in header value: 'Bearer # DeepSeek API Key\\n...'`。
    规则：丢掉空行和 # / // 开头的注释行，取最后一行。
    """
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    p = SECRETS_DIR / file_name
    if not p.exists():
        return ""
    lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#") and not ln.startswith("//")]
    return lines[-1] if lines else ""


def _env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """读一个整数环境变量：空串/非法值回落默认，再夹到 [lo, hi]。

    以前全是裸 `int(os.environ.get(...))` —— `.env` 里写一行 `AGENT_MAX_ROUNDS=`
    （或者把值注释掉）就会在 import 期抛 ValueError，整个 app 起不来，
    报错只说 config.py 里 int() 失败，看不出是哪个变量。
    `AGENT_MAX_ROUNDS=-1` 更阴：`range(max_rounds+1)` 变成空循环，接口返回一条空白回答。
    """
    raw = os.environ.get(name, "")
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _env_float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    raw = os.environ.get(name, "")
    try:
        x = float(str(raw).strip())
    except (TypeError, ValueError):
        x = default
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _env_flag(name: str, default: bool = True) -> bool:
    """布尔环境变量：认 1/true/yes/on。

    以前写 `os.environ.get(name, "1") == "1"` —— `.env` 里写 `AGENT_SHARED_MEMORY=true`
    会被静默判成 False，「配了没生效」还查不出原因。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


SILICONFLOW_KEY = _read_secret("SILICONFLOW_API_KEY", "siliconflow-api-key.txt")
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024

# A3 路由实验用：免费池里的小模型（0 元，但排队、能力弱）。
# 本机另测过 0.5B / glm-4-9b 都不可用（Model does not exist / Model disabled）。
SMALL_MODEL = (os.environ.get("SMALL_MODEL") or "").strip() or "Qwen/Qwen2.5-7B-Instruct"

# A2 用：DeepSeek 的对话模型（function calling）
DEEPSEEK_KEY = _read_secret("DEEPSEEK_API_KEY", "deepseek-api-key.txt")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
# 空串会让上游 400（然后被重试三次，见 agent._chat），所以这里必须兜底。
DEEPSEEK_MODEL = (os.environ.get("DEEPSEEK_MODEL") or "").strip() or "deepseek-chat"

# --- A2 的记忆预算（2026-11-12 放宽，用户要求「别删它记忆」）---
# 以前是 3 轮 / 200 字 / 120 字 / 5 条，纯为省钱；现在历史放宽到能真的记住一段对话。
# 全部可用环境变量覆盖，cost 失控时可以随时在 .env 里拧回来，不用改代码。
#
# 为什么「工具结果截断」只从 120 放到 240、没有放到 400：
#   实测过 400 字 / 8 条那组——50 条评测 47/50，还多出一次误触发（「上一条图是谁发的」不该调却调了）；
#   240 字 / 6 条这组是 48/50、误触发 0/12、平均 2.8 秒。
#   结论：该放宽的是**历史轮数**（它解的是指代，是记忆），不是**工具输出长度**（那只是噪音）。
AGENT_HISTORY_TURNS = _env_int("AGENT_HISTORY_TURNS", 20, lo=0, hi=200)   # 往后带几轮对话（以前 3）
AGENT_HISTORY_CHARS = _env_int("AGENT_HISTORY_CHARS", 600, lo=0, hi=4000)  # 单条历史截断（以前 200）
AGENT_HIT_CHARS = _env_int("AGENT_HIT_CHARS", 240, lo=0, hi=2000)          # 单条检索结果截断（以前 120）
AGENT_MAX_HITS = _env_int("AGENT_MAX_HITS", 6, lo=1, hi=12)               # 一次工具最多返回几条（以前 5）
AGENT_MAX_ROUNDS = _env_int("AGENT_MAX_ROUNDS", 3, lo=0, hi=6)            # 最多几轮工具调用（不动，多给一轮只会让它多绕）

# 「口令即身份」（v0.18）：同一个口令 = 同一段对话记忆，手机和电脑接得上。
# 之前按浏览器（cookie）分会话，结果是「手机上问一半，电脑上接不上」——
# 这套东西只有一个使用者，按浏览器分没有意义。
# 想退回旧行为：.env 里写 AGENT_SHARED_MEMORY=0。
AGENT_SHARED_MEMORY = _env_flag("AGENT_SHARED_MEMORY", True)

# ---- 融合参数（RRF）----
# 数据从 483 涨到 564 条之后，「让鱼帮我把人开盒」这条从第 4 名掉出了前 5：
# 向量那路把它排第 1，但关键词那路多出一堆「字面沾边」的候选，RRF 一平均就挤掉了。
# 所以把这两只旋钮露出来，要不要调由 scripts/bench_fusion.py 在两套评测集上验，
# 不是拍脑袋改（默认仍然 1.0 / 60，等于现在的行为）。
RRF_K = _env_int("RRF_K", 60, lo=1, hi=1000)              # 名次折扣常数，越大越平
RRF_VEC_W = _env_float("RRF_VEC_W", 1.5, lo=0.0, hi=10.0)  # 向量那一路的权重（关键词那一路固定 1.0）

# ---- 多查询扩展（v0.17）----
# 难评测集里 48% 的问题和答案是零字面重叠的（「二选一挑饮料」vs「百事还是可口」），
# 关键词那一路对它们天生 0 命中。做法：让模型把问题改写成几种「群友会打的说法」，
# 只拿去跑关键词那一路（几乎不花钱），向量那一路仍只用原句（省一次 embedding 调用）。
# 默认关：新东西先在小流量上验、把数字摆出来，再决定开不开。
MULTI_QUERY = _env_flag("MULTI_QUERY", False)
MQ_MAX = _env_int("MQ_MAX", 3, lo=1, hi=8)             # 最多扩展出几条
MQ_WEIGHT = _env_float("MQ_WEIGHT", 0.7, lo=0.0, hi=5.0)  # 扩展那几路的 RRF 权重（原句 1.0）
# 提示词风格：generic=「这句话还能怎么说」（第一版，实测负收益）；
# record=「记录里那句话长什么样」（第二版，冲着答案侧的字去）
MQ_PROMPT = os.environ.get("MQ_PROMPT", "generic")

# ---- 消息句意索引（v0.17）----
# 查询侧改了四版全是负收益，说明缺的字在**答案侧**：群里说的是「不@他是不会说话的」，
# 用户问的是「鱼什么时候才开口」。所以反过来，给每条消息补一句「它在说什么」，
# 把这句话单独建一路 FTS，查询时多一路召回（`scripts/gloss_messages.py` 负责生成）。
# 默认关：先在同一份快照上比出数字，再决定开不开。
GLOSS_ROUTE = _env_flag("GLOSS_ROUTE", False)
GLOSS_W = _env_float("GLOSS_W", 1.0, lo=0.0, hi=5.0)   # 句意那一路的 RRF 权重

# ---- 真浏览器兜底（v0.18.1）----
# 服务器抓不到 403 / JS 壳页面时，把 URL 转发给「本机浏览器阅读服务」。
# 那个服务跑在主人这台 Windows 上（Desktop\bsk-reader\bsk_reader.py），
# 通过 `ssh -N -R 8010:127.0.0.1:8010 gb` 出现在服务器的 127.0.0.1:8010。
# 电脑关着 / 隧道没开时探活 2 秒失败，行为与没有这个功能完全一致。
BROWSER_READER = os.environ.get("BROWSER_READER_URL", "http://127.0.0.1:8010").strip()

# ---- B 站选片（v0.19）---
# 「先看简介和评论（免费接口），值得再深读页面（要过主人电脑上的浏览器，贵），
#   最后写成 json 文件交给 AstrBot 推 QQ」。
# outbox 必须是 AstrBot 容器的 /AstrBot/data 下面，因为那是宿主与容器共享的目录：
#   /opt/qqbot/astrbot-data/curator/outbox  ==  /AstrBot/data/curator/outbox
# 本机调试时退回 data/curator/outbox，免得真去写一个不存在的路径。
CURATOR_OUTBOX = os.environ.get(
    "CURATOR_OUTBOX",
    "/opt/qqbot/astrbot-data/curator/outbox" if os.name != "nt" else str(DATA_DIR / "curator" / "outbox"),
)
CURATOR_DEEP_PER_DAY = _env_int("CURATOR_DEEP_PER_DAY", 3, lo=0)
CURATOR_PUSH_PER_HOUR = _env_int("CURATOR_PUSH_PER_HOUR", 1, lo=0)
CURATOR_SCORE_PUSH = _env_int("CURATOR_SCORE_PUSH", 4, lo=1, hi=5)
CURATOR_WINDOW = (_env_int("CURATOR_WINDOW_START", 8, lo=0, hi=23),
                  _env_int("CURATOR_WINDOW_END", 23, lo=1, hi=24))
# ---- B 站账号操作（v0.20）----
# cookie 优先从库里读（扫码登录写进去的），这个环境变量只是兜底。
BILI_COOKIE = os.environ.get("BILI_COOKIE", "").strip()
# 自动发评论：默认关。开着的时候，curator 选中的视频会直接用第一条草稿发出去。
BILI_AUTOPOST = _env_flag("BILI_AUTOPOST", False)
# 每天最多发几条评论（含自动与手动），防手滑刷屏。
BILI_POST_PER_DAY = _env_int("BILI_POST_PER_DAY", 5, lo=0)
# 同一个视频只发一条（同一个 aid 重复评论最容易被判定为刷屏）。
BILI_ONE_PER_VIDEO = _env_flag("BILI_ONE_PER_VIDEO", True)

# /out/ 里那些「要发出去的文件」（视频、封面）：投稿页要跨域 fetch 它们，
# 跨域请求带不上口令 cookie，所以用查询参数 token 换授权。配了才生效。
OUT_TOKEN = os.environ.get("OUT_TOKEN", "").strip()

# ---- 群内自学（A6，2026-11-19）----
# AstrBot 插件要调 /api/lexicon/*，它是容器里的进程，带不上口令 cookie，
# 所以这几个接口用 header 里的固定 token 放行（`X-GB-Token`）。
# 默认复用 OUT_TOKEN，少一个要配的东西；想分开就单独写 LEARN_TOKEN。
LEARN_TOKEN = (os.environ.get("LEARN_TOKEN") or OUT_TOKEN).strip()
# 每日花销上限：判词是白送的，真正花钱的是「上网查 + 模型写解释」，
# 所以卡的是「学会几个」而不是「判几次」。
LEX_MAX_TERMS_DAY = _env_int("LEX_MAX_TERMS_DAY", 40, lo=0)
LEX_MAX_LOOKUPS_DAY = _env_int("LEX_MAX_LOOKUPS_DAY", 12, lo=0)
# A6 接进 A2 的两个开关（2026-11-19 加的，为了 A/B）：
# 开了词库提示之后，服务器评测从 0.960/0 掉到 0.900/0.167——
# 多一个工具会让模型更爱循环调用。所以拆成两个旋钮分别量：
#   LEXICON_HINT  把已学词的解释注入 system（被动、不加工具）
#   LEXICON_TOOL  给模型 lookup_lexicon 工具（主动，会多一次「我该不该查」的判断）
LEXICON_HINT = _env_flag("LEXICON_HINT", True)
LEXICON_TOOL = _env_flag("LEXICON_TOOL", False)