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


SILICONFLOW_KEY = _read_secret("SILICONFLOW_API_KEY", "siliconflow-api-key.txt")
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024

# A3 路由实验用：免费池里的小模型（0 元，但排队、能力弱）。
# 本机另测过 0.5B / glm-4-9b 都不可用（Model does not exist / Model disabled）。
SMALL_MODEL = os.environ.get("SMALL_MODEL", "Qwen/Qwen2.5-7B-Instruct")

# A2 用：DeepSeek 的对话模型（function calling）
DEEPSEEK_KEY = _read_secret("DEEPSEEK_API_KEY", "deepseek-api-key.txt")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# --- A2 的记忆预算（2026-11-12 放宽，用户要求「别删它记忆」）---
# 以前是 3 轮 / 200 字 / 120 字 / 5 条，纯为省钱；现在历史放宽到能真的记住一段对话。
# 全部可用环境变量覆盖，cost 失控时可以随时在 .env 里拧回来，不用改代码。
#
# 为什么「工具结果截断」只从 120 放到 240、没有放到 400：
#   实测过 400 字 / 8 条那组——50 条评测 47/50，还多出一次误触发（「上一条图是谁发的」不该调却调了）；
#   240 字 / 6 条这组是 48/50、误触发 0/12、平均 2.8 秒。
#   结论：该放宽的是**历史轮数**（它解的是指代，是记忆），不是**工具输出长度**（那只是噪音）。
AGENT_HISTORY_TURNS = int(os.environ.get("AGENT_HISTORY_TURNS", "20"))  # 往后带几轮对话（以前 3）
AGENT_HISTORY_CHARS = int(os.environ.get("AGENT_HISTORY_CHARS", "600"))  # 单条历史截断（以前 200）
AGENT_HIT_CHARS = int(os.environ.get("AGENT_HIT_CHARS", "240"))          # 单条检索结果截断（以前 120）
AGENT_MAX_HITS = int(os.environ.get("AGENT_MAX_HITS", "6"))              # 一次工具最多返回几条（以前 5）
AGENT_MAX_ROUNDS = int(os.environ.get("AGENT_MAX_ROUNDS", "3"))          # 最多几轮工具调用（不动，多给一轮只会让它多绕）

# 「口令即身份」（v0.18）：同一个口令 = 同一段对话记忆，手机和电脑接得上。
# 之前按浏览器（cookie）分会话，结果是「手机上问一半，电脑上接不上」——
# 这套东西只有一个使用者，按浏览器分没有意义。
# 想退回旧行为：.env 里写 AGENT_SHARED_MEMORY=0。
AGENT_SHARED_MEMORY = os.environ.get("AGENT_SHARED_MEMORY", "1") == "1"

# ---- 融合参数（RRF）----
# 数据从 483 涨到 564 条之后，「让鱼帮我把人开盒」这条从第 4 名掉出了前 5：
# 向量那路把它排第 1，但关键词那路多出一堆「字面沾边」的候选，RRF 一平均就挤掉了。
# 所以把这两只旋钮露出来，要不要调由 scripts/bench_fusion.py 在两套评测集上验，
# 不是拍脑袋改（默认仍然 1.0 / 60，等于现在的行为）。
RRF_K = int(os.environ.get("RRF_K", "60"))            # 名次折扣常数，越大越平
RRF_VEC_W = float(os.environ.get("RRF_VEC_W", "1.5"))  # 向量那一路的权重（关键词那一路固定 1.0）

# ---- 多查询扩展（v0.17）----
# 难评测集里 48% 的问题和答案是零字面重叠的（「二选一挑饮料」vs「百事还是可口」），
# 关键词那一路对它们天生 0 命中。做法：让模型把问题改写成几种「群友会打的说法」，
# 只拿去跑关键词那一路（几乎不花钱），向量那一路仍只用原句（省一次 embedding 调用）。
# 默认关：新东西先在小流量上验、把数字摆出来，再决定开不开。
MULTI_QUERY = os.environ.get("MULTI_QUERY", "0") == "1"
MQ_MAX = int(os.environ.get("MQ_MAX", "3"))            # 最多扩展出几条
MQ_WEIGHT = float(os.environ.get("MQ_WEIGHT", "0.7"))  # 扩展那几路的 RRF 权重（原句 1.0）
# 提示词风格：generic=「这句话还能怎么说」（第一版，实测负收益）；
# record=「记录里那句话长什么样」（第二版，冲着答案侧的字去）
MQ_PROMPT = os.environ.get("MQ_PROMPT", "generic")

# ---- 消息句意索引（v0.17）----
# 查询侧改了四版全是负收益，说明缺的字在**答案侧**：群里说的是「不@他是不会说话的」，
# 用户问的是「鱼什么时候才开口」。所以反过来，给每条消息补一句「它在说什么」，
# 把这句话单独建一路 FTS，查询时多一路召回（`scripts/gloss_messages.py` 负责生成）。
# 默认关：先在同一份快照上比出数字，再决定开不开。
GLOSS_ROUTE = os.environ.get("GLOSS_ROUTE", "0") == "1"
GLOSS_W = float(os.environ.get("GLOSS_W", "1.0"))      # 句意那一路的 RRF 权重

