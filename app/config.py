"""全局路径与配置。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "group_brain.db"
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
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024

# A2 用：DeepSeek 的对话模型（function calling）
DEEPSEEK_KEY = _read_secret("DEEPSEEK_API_KEY", "deepseek-api-key.txt")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
