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

-- A4 学习教练的周报。week 用 ISO 周（2026-W46），一周一份，重算就覆盖。
CREATE TABLE IF NOT EXISTS reports (
    week         TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    facts        TEXT NOT NULL,   -- 喂给模型之前的那份「事实清单」，留档是为了以后能查「它是不是在编」
    text         TEXT NOT NULL,   -- 模型写出来的周报正文
    model        TEXT
);

-- A2 的对话记忆（2026-11-12）：以前「刚才那句」只存在浏览器内存里，刷新就没了。
-- 现在按 session 存服务端，换设备/刷新都还在。session 是浏览器 cookie 里的随机串。
CREATE TABLE IF NOT EXISTS agent_turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    role       TEXT NOT NULL,   -- 只有 user / assistant 两种，写库前就白名单过滤
    content    TEXT NOT NULL
);

-- 纠错记忆（2026-11-12，v0.13）：它答错过的地方，用户点一下「不对，我教它」，
-- 下次同一个问题直接照用户给的答案答。
-- 和 agent_turns 的区别：agent_turns 是「流水账」（发生过什么），
-- corrections 是「结论」（应该怎么答）。流水账不会改变行为，结论会。
-- norm 是 question 归一化后的串（去标点空白、全角转半角、小写），用来做精确/包含匹配；
-- source 记「这条是谁教的」，将来区分「网页纠正」和「群里的纠正」。
CREATE TABLE IF NOT EXISTS corrections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    question   TEXT NOT NULL,   -- 用户当时问的那句话，原样
    norm       TEXT NOT NULL,   -- 归一化后的，只用来匹配
    wrong      TEXT,            -- 它当时答错的（可能为空）
    right      TEXT NOT NULL,   -- 用户给的正确答案
    source     TEXT,            -- web / qq / admin
    session_id TEXT,
    hits       INTEGER NOT NULL DEFAULT 0   -- 被用过几次，用来判断这条还有没有用
);

-- 事实记忆（v0.14）：从群聊里抽「谁 / 什么 是什么样」，先挂 pending，
-- 用户审过（approved）才会被检索到。为什么必须审：抽取是模型干的，模型会编——
-- 一条编出来的事实会被当成「群里的常识」反复引用，比一次答错的伤害大得多。
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,     -- 抽取时间（不是消息时间）
    subject    TEXT NOT NULL,     -- 谁 / 什么，如「147」「大肥鱼」「群名」
    claim      TEXT NOT NULL,     -- 结论，如「爱喝扎啤」
    quote      TEXT,              -- 依据的原话（截断），方便人工核对
    msg_ids    TEXT,              -- 依据的消息 id，逗号分隔
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / rejected
    hits       INTEGER NOT NULL DEFAULT 0,
    decided_at TEXT
);

-- 规则自学习（v0.15）：从评测语料里挖「某种问法一定该调某个工具」，
-- 只把留出集上不掉准确率的写进来；enabled=0 表示挖出来但没启用。
CREATE TABLE IF NOT EXISTS rules (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern   TEXT NOT NULL,      -- 正则片段，re.search 用的那种
    tool      TEXT NOT NULL,      -- 命中就前置调这个工具
    evidence  INTEGER NOT NULL DEFAULT 0,   -- 支持它的题有几条
    precision REAL NOT NULL DEFAULT 0,      -- 在语料上的准确率
    note      TEXT,
    enabled   INTEGER NOT NULL DEFAULT 0,
    ts        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_name);
CREATE INDEX IF NOT EXISTS idx_memes_tag ON memes(tag);
CREATE INDEX IF NOT EXISTS idx_agent_turns_session ON agent_turns(session_id, id);
CREATE INDEX IF NOT EXISTS idx_corrections_norm ON corrections(norm);
CREATE INDEX IF NOT EXISTS idx_facts_status ON facts(status);
CREATE INDEX IF NOT EXISTS idx_rules_enabled ON rules(enabled);

-- 事实的关键词索引（bigram，和 messages 用同一套切法）。只索引 approved：
-- pending 的东西用户还没看过，不该能被检索到。
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    bigram,
    fact_id UNINDEXED,
    tokenize = "unicode61"
);
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

