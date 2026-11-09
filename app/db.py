"""SQLite 建表与连接。

两张正表 + 两张 FTS5 索引表 + 一张向量表（v0.4 才用）。

FTS 表故意做成"自带 rowid"的普通 fts5 表，只多存一个 UNINDEXED 的原始 id，
这样 DELETE FROM xxx_fts 就能整体重建，不用折腾 external content 表。
"""
from __future__ import annotations

import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    ts          TEXT,
    session     TEXT,
    sender_id   TEXT,
    sender_name TEXT,
    kind        TEXT,          -- user / bot
    text        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memes (
    id       INTEGER PRIMARY KEY,
    pack     TEXT,
    tag      TEXT,
    caption  TEXT,
    keywords TEXT,
    path     TEXT UNIQUE,
    file_name TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    bigram,
    msg_id UNINDEXED,
    tokenize = "unicode61"
);

CREATE VIRTUAL TABLE IF NOT EXISTS memes_fts USING fts5(
    bigram,
    meme_id UNINDEXED,
    tokenize = "unicode61"
);

CREATE TABLE IF NOT EXISTS vectors (
    kind     TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL,
    PRIMARY KEY (kind, owner_id)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_name);
CREATE INDEX IF NOT EXISTS idx_memes_tag ON memes(tag);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def set_meta(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, v),
    )


def get_meta(conn: sqlite3.Connection, k: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else default


def window(conn: sqlite3.Connection, msg_id: int, before: int = 3, after: int = 3) -> list[sqlite3.Row]:
    """取某条消息前后各 N 条，按时间正序返回，供「看上下文」用（v0.2 记忆回放）。

    为什么不用 id 排序：id 是导入顺序，理论上和 ts 一致，但导入是分批跑的
    （先 484 条、后补数据），id 顺序和真实发言顺序不保证吻合。ts 才是事实。
    同一秒内的多条靠 id 兜底，避免排序不稳定导致「同一条消息忽上忽下」。
    """
    me = conn.execute("SELECT id, ts, session FROM messages WHERE id = ?", (msg_id,)).fetchone()
    if me is None:
        return []

    # 同一会话内取窗口；session 为空时退化成全库（历史数据里 session 可能没填）
    where = "AND session = ?" if me["session"] else ""
    args_me = (me["ts"], me["id"]) if me["session"] else (me["ts"], me["id"])
    tail = (me["session"],) if me["session"] else ()

    prev = conn.execute(
        f"""SELECT id, ts, sender_name, kind, text FROM messages
            WHERE (ts, id) <= (?, ?) {where}
            ORDER BY ts DESC, id DESC LIMIT ?""",
        (*args_me, *tail, before + 1),
    ).fetchall()

    nxt = conn.execute(
        f"""SELECT id, ts, sender_name, kind, text FROM messages
            WHERE (ts, id) > (?, ?) {where}
            ORDER BY ts ASC, id ASC LIMIT ?""",
        (*args_me, *tail, after),
    ).fetchall()

    return list(reversed(prev)) + list(nxt)

