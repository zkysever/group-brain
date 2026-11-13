"""CI 里跑的自检：不联网、不需要真数据，只验证「代码还能跑通」这件事。

为什么要有它：这个项目的真正测试是 eval/ 里那两套评测，但它们需要真实群聊
数据库（4.8MB，不进 git）和 API key，CI 上跑不了。于是退一步，用临时库把
「建表 → 写入 → 关键词检索 → 取上下文 → 统计」这条主链路走一遍。

它拦住的是这类事故：
  - 改了 SQL / 表结构，忘了迁移 → 建表或查询报错
  - 改了 search / db 的函数签名，调用方没跟着改 → ImportError / TypeError
  - 分词函数写崩了 → 检索返回空

用法：
    python scripts/selftest.py
    GB_DB=/tmp/ci.db python scripts/selftest.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前设置：config 在导入时就把 DB_PATH 定下来了。
if not os.environ.get("GB_DB"):
    os.environ["GB_DB"] = str(Path(tempfile.gettempdir()) / "gb_selftest.db")

from app import db, search  # noqa: E402
from app.config import DB_PATH  # noqa: E402



FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [ok] " if ok else "  [X]  ") + name + (f"    {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    db.init()
    conn = db.connect()
    try:
        # 1. 建表
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
        need = {"messages", "memes", "vectors", "meta", "reports", "messages_fts", "memes_fts"}
        check("建表", need <= tables, "缺: " + ", ".join(sorted(need - tables)) if need - tables else "")

        # 2. 写入两条消息 + 一张表情包
        rows = [
            ("2026-01-01 10:00:00.000000", "147", "user", "@大肥鱼 我想吃鱼片", None),
            ("2026-01-01 10:00:30.000000", "bot", "bot", "鱼片的花样可太多了——酸菜鱼、水煮鱼", None),
        ]
        cur = conn.executemany(
            "INSERT INTO messages (ts, sender_name, kind, text, session) VALUES (?,?,?,?,?)", rows
        )
        conn.execute(
            "INSERT INTO memes (tag, path, caption, keywords) VALUES (?,?,?,?)",
            ("angry", "pack/memes/angry/1.png", "鼓起脸生气，冒气", "生气 愤怒 哼"),
        )
        conn.commit()
        check("写入消息", cur.rowcount == 2, f"{cur.rowcount} 条")

        # 3. FTS 索引：用和导入脚本同一套分词写法灌一遍
        from app.text import to_bigrams

        for r in conn.execute("SELECT id, sender_name, text FROM messages").fetchall():
            conn.execute(
                "INSERT INTO messages_fts (bigram, msg_id) VALUES (?, ?)",
                (to_bigrams(r["text"] + " " + (r["sender_name"] or "")), r["id"]),
            )
        for r in conn.execute("SELECT id, caption, keywords FROM memes").fetchall():
            conn.execute(
                "INSERT INTO memes_fts (bigram, meme_id) VALUES (?, ?)",
                (to_bigrams((r["caption"] or "") + " " + (r["keywords"] or "")), r["id"]),
            )
        conn.commit()

        # 4. 关键词检索（最高频的用法）
        hit = search.search_messages(conn, "鱼片", limit=5)
        check("搜消息「鱼片」", len(hit) >= 1, f"{len(hit)} 条")
        hit2 = search.search_memes(conn, "生气", limit=5)
        check("搜表情包「生气」", len(hit2) >= 1, f"{len(hit2)} 条")

        # 5. 混合检索在没向量时必须老实退回关键词（不能报错、不能返回空）
        mixed = search.search_all(conn, "鱼片", mode="auto", limit=5)
        check("auto 模式无向量时退回关键词", len(mixed) >= 1, f"{len(mixed)} 条")

        # 6. 取上下文
        mid = conn.execute("SELECT id FROM messages ORDER BY id LIMIT 1").fetchone()["id"]
        win = db.window(conn, mid, before=1, after=1)
        check("取上下文 window()", len(win) == 2, f"{len(win)} 条")

        # 7. 统计接口用到的 stats()
        st = search.stats(conn)
        check("stats()", st.get("messages") == 2 and st.get("memes") == 1, str(st))

        # 8. 服务端记忆（2026-11-12 加的）：写两轮、读回来、数轮数、删干净
        from app import memory

        sid = memory.new_session()
        memory.append(conn, sid, "user", "谁说过要吃鱼片")
        memory.append(conn, sid, "assistant", "是 147 说的")
        memory.append(conn, sid, "system", "这条应该被白名单扔掉")  # role 不合法 → 忽略
        past = memory.recent(conn, sid, 20)
        check(
            "memory 写入/读取",
            len(past) == 2 and past[0]["role"] == "user" and past[1]["role"] == "assistant",
            f"{len(past)} 条",
        )
        check("memory 轮数", memory.turn_count(conn, sid) == 1, str(memory.turn_count(conn, sid)))
        check("memory 清空", memory.forget(conn, sid) == 2 and memory.recent(conn, sid, 20) == [])

        # 9. 增量导入：同一个源导两次，第二次必须一条不新增（这是「别再删记忆」的底线）
        import sqlite3

        from scripts.import_messages import import_from

        src = Path(tempfile.gettempdir()) / "gb_selftest_src.db"
        src.unlink(missing_ok=True)
        s = sqlite3.connect(src)
        s.execute(
            "CREATE TABLE platform_message_history (id INTEGER PRIMARY KEY, created_at TEXT,"
            " user_id TEXT, sender_id TEXT, sender_name TEXT, content TEXT)"
        )
        for i, t in ((101, "我想吃鱼片"), (102, "晚安")):
            s.execute(
                "INSERT INTO platform_message_history VALUES(?,?,?,?,?,?)",
                (i, f"2026-01-02 10:00:0{i - 100}.000000", "qq:Group", "u1", "147",
                 json.dumps({"type": "user", "message": [{"type": "plain", "text": t}]},
                            ensure_ascii=False)),
            )
        s.commit()
        s.close()
        r1 = import_from(src)
        r2 = import_from(src)
        check("增量导入：首次新增", r1["added"] == 2 and r1["skipped"] == 0, str(r1))
        check("增量导入：二次全跳过", r2["added"] == 0 and r2["skipped"] == 2, str(r2))
        src.unlink(missing_ok=True)

        # 10. 纠错记忆（v0.13）：教一句、能命中、同一句再教是覆盖不是叠加、能删
        from app import learn

        cid = learn.record(conn, "帮我代一个白系AP", "白系 AP 是《舞萌》里的一首歌", wrong="不知道")
        hit = learn.find(conn, "帮我代一个白系ap？")  # 只差大小写和标点，必须命中
        check("learn 归一化命中", bool(hit) and hit["id"] == cid, str(hit and hit["right"]))
        loose = learn.find(conn, "帮我代一个白系AP啊")  # 多一个字，靠包含规则命中
        check("learn 包含命中", bool(loose) and loose["right"].startswith("白系 AP"))
        check("learn 不会乱吃", learn.find(conn, "今天几号") is None)
        learn.record(conn, "帮我代一个白系AP", "改一版答案")
        check("learn 再教是覆盖", learn.stats(conn)["total"] == 1, str(learn.stats(conn)))
        check("learn 删掉", learn.delete(conn, cid) == 1 and learn.find(conn, "帮我代一个白系AP") is None)

        # 11. 事实记忆（v0.14）：pending 查不到 → 点头后查得到 → 相似草稿去重 → 拒绝后又查不到
        from app import facts as fx
        from app import router as rt
        from app import expand as ex

        r = fx.add_candidates(conn, [
            {"subject": "147", "claim": "爱喝扎啤", "quote": "扎啤", "msg_ids": [565]},
            {"subject": "147", "claim": "爱喝扎啤", "quote": "重复", "msg_ids": [566]},
            {"subject": "147", "claim": "爱喝扎啤啊", "quote": "扎啤", "msg_ids": [567]},
        ])
        check("facts 写入去重（全等 + 换一两个字）", r["added"] == 1 and r["dup"] == 2, str(r))
        # 换了一整套说法的「同一个意思」抓不到——这是 bigram 相似度的边界，写在 EVAL.md 里了
        r2 = fx.add_candidates(conn, [
            {"subject": "bot", "claim": "口头禅是杂口杂口和扎口", "quote": "杂口杂口", "msg_ids": [40]},
            {"subject": "bot", "claim": "常用扎口扎口作语气词", "quote": "扎口扎口", "msg_ids": [41]},
        ])
        check("facts 相似度抓不到换说法的（已知边界）", r2["added"] == 2, str(r2))
        check("facts pending 检索不到", fx.search(conn, "147 喝什么") == [])
        fid = conn.execute("SELECT id FROM facts WHERE subject='147'").fetchone()["id"]
        fx.decide(conn, fid, "approved")
        hit = fx.search(conn, "147 爱喝什么")
        check("facts 点头后能查到", bool(hit) and hit[0]["claim"] == "爱喝扎啤", str(hit[:1]))
        fx.decide(conn, fid, "rejected")
        check("facts 拒绝后又查不到", fx.search(conn, "147 爱喝什么") == [])

        # 12. 自学规则（v0.15）：enabled=1 才参与前置，且含指代词时不采信
        conn.execute("INSERT INTO rules (pattern, tool, evidence, precision, enabled, ts)"
                     " VALUES ('有人说过', 'search_memory', 2, 1.0, 1, '2026-11-12 00:00:00')")
        conn.commit()
        rules = rt.learned_rules(conn)
        check("rules 读到启用规则", len(rules) == 1, str(rules))
        t1 = rt.rule_route("有人说过这句话吗", rules=rules)
        check("rules 命中参与前置", t1[1] == "high" and "search_memory" in t1[0], str(t1))
        t2 = rt.rule_route("刚才那句话有人说过吗", rules=rules)
        check("rules 遇指代词降级", t2[1] == "low", str(t2))
        conn.execute("UPDATE rules SET enabled = 0")
        conn.commit()
        check("rules 停用后不采信", rt.learned_rules(conn) == [])

        # 13. 查询扩展（v0.16）：只测纯函数，不联网。
        #     功能实测无效（EVAL.md 第十三节），但代码要在，且解析不能崩。
        check("expand 归一化键", ex.norm_key(" 谁　说过「鱼片」？ ") == "谁说过鱼片")
        payload = json.dumps(["鱼片谁说的", "想吃鱼片", "鱼片谁说的", "长" * 40], ensure_ascii=False)
        alts = ex._parse(payload, "谁说过鱼片")
        check("expand 解析去重/去原句/截长度", alts == ["鱼片谁说的", "想吃鱼片"], str(alts))
        check("expand 解析非 JSON 返回空", ex._parse("没有数组", "q") == [])

        # 14. 事实自动审阅（v0.16）：机器只处理它确定的那批。
        conn.execute("INSERT INTO messages (id, ts, sender_name, kind, session, text)"
                     " VALUES (901, '2026-01-01 10:00:00', '147', 'user', 'g', '我想吃鱼片')")
        conn.execute("INSERT INTO messages (id, ts, sender_name, kind, session, text)"
                     " VALUES (902, '2026-01-01 10:00:10', 'bot', 'bot', 'g', '我是这个群里的 AI 助手')")
        conn.commit()
        fx.add_candidates(conn, [
            {"subject": "147", "claim": "想吃鱼片", "quote": "我想吃鱼片", "msg_ids": [901]},
            {"subject": "bot", "claim": "是群里的 AI 助手", "quote": "我是这个群里的 AI 助手", "msg_ids": [902]},
        ])
        ok = fx.auto_approve(conn, dry=True)
        bad = fx.auto_reject(conn, dry=True)
        check("审阅：真人那条自动点头", ok["picked"] == 1, str(ok["detail"]))
        # 垃圾名单里必须包含那条 bot 事实（别的「查不到原话」的老数据也会进来，不数条数）
        bot_row = conn.execute(
            "SELECT id FROM facts WHERE subject = 'bot' AND status = 'pending'"
        ).fetchone()
        check(
            "审阅：bot 那条被列入自动拒绝",
            bool(bot_row) and any(d["id"] == bot_row["id"] for d in bad["detail"]),
            str(bad["detail"]),
        )
        check("审阅：dry-run 不写库", fx.stats(conn)["approved"] == 0, str(fx.stats(conn)))
        fx.auto_approve(conn, dry=False)
        check("审阅：--apply 后真的生效", fx.stats(conn)["approved"] == 1, str(fx.stats(conn)))
        check("审阅：一键撤销退回待审", fx.undo_auto(conn) == 1 and fx.stats(conn)["approved"] == 0)
    finally:
        conn.close()

    print()
    if FAILS:
        print(f"[X] {len(FAILS)} 项没过：" + "、".join(FAILS))
        raise SystemExit(1)
    print("[OK] 自检全过（用到的是临时库，没碰你的数据）")


if __name__ == "__main__":
    main()
