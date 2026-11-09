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


def _read_secret(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    p = SECRETS_DIR / "siliconflow-api-key.txt"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


SILICONFLOW_KEY = _read_secret("SILICONFLOW_API_KEY")
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024
