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
