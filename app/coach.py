"""A4：学习教练——把一周的「事实」交给模型，让它写一份不给外人看的周报。

和 A2 的区别很关键：
  A2 是「用户问、模型查」，模型是主导者；
  A4 是「我先算好事实，模型只负责组织语言」，模型是编辑。

所以这里**不让模型调工具**，也不让它自己去 GitHub 上找东西。
先把这一周真正发生过的事抄成一份清单（提交、改动、群里的活跃天数），
再让它照着清单写三段：做了什么 / 卡在哪 / 下周一件具体的事。

这个顺序不能反。让模型「自己去看 GitHub 然后总结」，它会编——而且编得很像真的。
留档的 `facts` 就是为了事后能对账：报告里的每一句，都应该能在 facts 里找到出处。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

import requests

from . import config as cfg
from . import db

REPO = "zkysever/group-brain"
GH_API = "https://api.github.com"
MAX_COMMITS = 60          # 一周六十个提交以上就不必逐个列了，列不下也没人看
FACT_CHARS = 120          # 单条提交信息截断

SYSTEM = """你是一个程序员的学习教练。用户会给你一份「这一周的事实清单」，
你要据此写一份周报，给用户本人看（不是给领导看，不用客套，不用鼓励）。

硬规矩：
1. 只能用清单里的事实。清单里没有的东西，一个字都不许加。
2. 不要形容词堆砌。「完成了」「推进了」这种话没有信息量，直接说做了什么。
3. 卡点要从事实里推：修 bug / 回退 / 同一个文件反复改 / 提交信息里带 fix、revert、又、再次，都是线索。
4. 三段，每段一至两句，总计不超过 260 字：
   一、这周做了什么
   二、卡在哪（没有明显卡点就直说「没有明显卡点」，别硬凑）
   三、下周一件具体的事（要能直接开始做，不要「继续深入」这种）
5. 用中文，说人话。"""


# ---------------------------------------------------------------- 时间

def iso_week(d: date | None = None) -> str:
    d = d or date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_range(week: str) -> tuple[date, date]:
    """'2026-W46' → (周一, 周日)。用 ISO 周，跟 GitHub 上的周视图对得上。"""
    m = re.fullmatch(r"(\d{4})-W(\d{2})", week.strip())
    if not m:
        raise ValueError(f"周格式不对，应该是 2026-W46 这样：{week!r}")
    y, w = int(m.group(1)), int(m.group(2))
    try:
        monday = date.fromisocalendar(y, w, 1)
    except ValueError as e:
        raise ValueError(f"没有这一周：{week}") from e
    return monday, monday + timedelta(days=6)


# ---------------------------------------------------------------- 事实

def _gh_token() -> str:
    return cfg._read_secret("GITHUB_TOKEN", "github-pat.txt")


def fetch_commits(since: date, until: date) -> list[dict[str, Any]]:
    """拉一周的提交。这个仓库是公开的，没 token 也能拉，只是每小时只有 60 次。"""
    params = {
        "since": f"{since.isoformat()}T00:00:00Z",
        "until": f"{until.isoformat()}T23:59:59Z",
        "per_page": 100,
    }
    headers = {"Accept": "application/vnd.github+json"}
    tok = _gh_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    out: list[dict[str, Any]] = []
    url = f"{GH_API}/repos/{REPO}/commits"
    for _ in range(3):                      # 最多翻 3 页，一周 300 个提交已经够用了
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for c in batch:
            msg = (c["commit"]["message"] or "").strip()
            out.append({
                "sha": c["sha"][:7],
                "msg": msg.splitlines()[0][:FACT_CHARS],
                "body_lines": max(0, len(msg.splitlines()) - 1),
                "at": c["commit"]["author"]["date"],
            })
        if len(batch) < 100:
            break
        url = r.links.get("next", {}).get("url", "")
        if not url:
            break
        params = None                     # next 链接里已经带参数了
    return out[:MAX_COMMITS]


def chat_days(conn, since: date, until: date) -> dict[str, Any]:
    """群聊活跃度：只取「有几天有人说话」「一共几条」「谁最活跃」。不取原文。"""
    rows = conn.execute(
        "SELECT substr(ts, 1, 10) AS d, COUNT(*) AS n FROM messages "
        "WHERE substr(ts, 1, 10) BETWEEN ? AND ? GROUP BY d ORDER BY d",
        (since.isoformat(), until.isoformat()),
    ).fetchall()
    total = sum(int(r["n"]) for r in rows)
    top = conn.execute(
        "SELECT sender_name, COUNT(*) AS n FROM messages "
        "WHERE substr(ts, 1, 10) BETWEEN ? AND ? AND sender_name != 'bot' "
        "GROUP BY sender_name ORDER BY n DESC LIMIT 3",
        (since.isoformat(), until.isoformat()),
    ).fetchall()
    return {
        "days_active": len(rows),
        "total": total,
        "busiest": max(((r["d"], int(r["n"])) for r in rows), default=("", 0)),
        "top_senders": [f"{r['sender_name']}({r['n']})" for r in top],
    }


def collect_facts(conn, week: str) -> dict[str, Any]:
    since, until = week_range(week)
    commits = fetch_commits(since, until)
    chat = chat_days(conn, since, until)

    fixish = [c for c in commits if re.search(r"fix|修|回退|revert|又|再次|还是|没生效", c["msg"], re.I)]
    files_hint = sorted({
        f for c in commits for f in re.findall(r"([\w/]+\.(?:py|js|html|md|yml|sh))", c["msg"])
    })
    return {
        "week": week,
        "from": since.isoformat(),
        "to": until.isoformat(),
        "commits": commits,
        "commit_count": len(commits),
        "fix_like": [c["msg"][:80] for c in fixish[:6]],
        "touched_hint": files_hint[:8],
        "chat": chat,
    }


def render_facts(facts: dict[str, Any]) -> str:
    """把事实清单拍成给模型看的一段纯文本。刻意不用 JSON——模型对着 JSON 容易照着念字段名。"""
    lines = [
        f"周期：{facts['from']} ~ {facts['to']}（{facts['week']}）",
        f"提交次数：{facts['commit_count']}",
    ]
    if facts["commits"]:
        lines.append("提交列表（新的在上）：")
        for c in facts["commits"]:
            lines.append(f"  - {c['at'][:10]} {c['msg']}")
    else:
        lines.append("提交列表：这一周没有提交。")

    if facts["fix_like"]:
        lines.append("看起来像在返工的提交：" + "；".join(f"「{m}」" for m in facts["fix_like"]))
    if facts["touched_hint"]:
        lines.append("提交信息里出现过这些文件名：" + "、".join(facts["touched_hint"]))

    ch = facts["chat"]
    lines.append(
        f"群聊：{ch['days_active']} 天有人说话，一共 {ch['total']} 条"
        + (f"，最热闹是 {ch['busiest'][0]}（{ch['busiest'][1]} 条）" if ch["busiest"][0] else "")
    )
    if ch["top_senders"]:
        lines.append("说话最多：" + "、".join(ch["top_senders"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- 生成

def _call_llm(user_text: str) -> str:
    if not cfg.DEEPSEEK_KEY:
        raise RuntimeError("没配 DEEPSEEK_API_KEY，写不了周报")
    payload = {
        "model": cfg.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.4,
    }
    headers = {"Authorization": f"Bearer {cfg.DEEPSEEK_KEY}"}
    r = requests.post(cfg.DEEPSEEK_URL, json=payload, headers=headers, timeout=90)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


def generate(conn, week: str = "", save: bool = True) -> dict[str, Any]:
    week = (week or iso_week()).strip()
    facts = collect_facts(conn, week)
    text = _call_llm(render_facts(facts))
    if save:
        conn.execute(
            "INSERT INTO reports(week, generated_at, facts, text, model) VALUES(?,?,?,?,?) "
            "ON CONFLICT(week) DO UPDATE SET generated_at=excluded.generated_at, "
            "facts=excluded.facts, text=excluded.text, model=excluded.model",
            (week, datetime.now().isoformat(timespec="seconds"),
             render_facts(facts), text, cfg.DEEPSEEK_MODEL),
        )
        conn.commit()
    return {"ok": True, "week": week, "text": text, "facts": facts}


def get(conn, week: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM reports WHERE week=?", (week,)).fetchone()
    return dict(row) if row else None


def recent(conn, limit: int = 12) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT week, generated_at, model, text FROM reports ORDER BY week DESC LIMIT ?", (limit,),
    ).fetchall()
    out = []
    for r in rows:
        # 列表里只要一行摘要：去掉 ** 标记、把换行压成空格，不然前端那行会被撑开
        head = re.sub(r"[*#`>]+", "", r["text"]).replace("\n", " ").strip()
        out.append({"week": r["week"], "generated_at": r["generated_at"],
                    "model": r["model"], "head": head[:120]})
    return out
