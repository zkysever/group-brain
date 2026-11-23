"""把已发布稿件的封面换成新版（第 04 版「电影海报」）。

踩过的三个坑（都在这份脚本里处理了）：
  1. 「完成」按钮是 `div.button.submit`，不是 `<button>`，按标签找永远找不到；
  2. 用 JS 造 DataTransfer 塞 input.files，B 站不认（预览不出现）——要用
     CDP 的 DOM.setFileInputFiles 走真实文件选择；
  3. 点完「完成」可能弹「确认同步」（4:3 / 16:9 两个比例），要点掉再「提交」。

用法：
    python tools/set_cover.py --aid 117660931066948 --cover out/covers_new/ep13_cover.png
    python tools/set_cover.py --all
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import edge_cdp as e

ROOT = Path(__file__).resolve().parent.parent
EDIT = "https://member.bilibili.com/platform/upload/video/frame?aid={aid}&type=edit"

JS_HELPERS = r"""
window.__gb = {
  t: (n) => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim(),
  vis: (n) => !!(n.offsetParent || n.getClientRects().length),
  dlg: () => [...document.querySelectorAll('.bcc-dialog, [class*=dialog]')]
             .filter(n => window.__gb.vis(n) && /封面制作/.test(n.innerText || ''))
             .pop(),
  find: (txt, scope) => {
    const root = scope || document;
    const cands = [...root.querySelectorAll('*')]
      .filter(n => window.__gb.vis(n) && window.__gb.t(n) === txt);
    const leaf = cands.filter(n => ![...n.children].some(c => window.__gb.t(c) === txt));
    return leaf[0] || cands[cands.length - 1] || null;
  },
  click: (txt, scope) => {
    const n = window.__gb.find(txt, scope);
    if (!n) return {ok: false, want: txt};
    n.click();
    return {ok: true, txt};
  },
};
'ok';
"""


def log(m: str) -> None:
    print(f"[cover] {m}", flush=True)


def close_extra_dialogs() -> list[str]:
    closed: list[str] = []
    for _ in range(3):
        r = e.js(
            r"""
(() => {
  for (const d of [...document.querySelectorAll('.bcc-dialog, [class*=dialog]')]) {
    if (!d.getClientRects().length) continue;
    if (/封面制作/.test(d.innerText || '')) continue;
    for (const w of ['暂不考虑', '取消', '知道了']) {
      const b = window.__gb.find(w, d);
      if (b) { b.click(); return {c: w}; }
    }
  }
  return {c: null};
})()
"""
        )
        if not r or not r.get("c"):
            break
        closed.append(r["c"])
        time.sleep(1.0)
    return closed


def dialog_open() -> bool:
    return bool(e.js("!!window.__gb.dlg()"))


def open_dialog(seconds: float = 25.0) -> bool:
    end = time.time() + seconds
    while time.time() < end and not e.js("!!document.querySelector('.cover-img')"):
        time.sleep(1.0)
    if not e.js("!!document.querySelector('.cover-img')"):
        log("等不到 .cover-img")
        return False
    for _ in range(4):
        for sel in (".cover-img", ".cover-slot", ".cover-module"):
            e.js(f"(()=>{{const n=document.querySelector('{sel}'); if(n) n.click(); return !!n;}})()",
                 await_promise=False)
            time.sleep(1.5)
            if dialog_open():
                log(f"弹窗开了（{sel}）")
                return True
        time.sleep(2.0)
    return False


def set_file(path: Path) -> dict:
    """把封面文件塞进 file input。

    坑：DOM 域状态按连接记，所以整段必须挂在**同一条** CDP 连接上
    （分多次开连接时 DOM.requestNode 会返回 null）。
    """
    with e.session() as c:
        for sel in ('input[type=file][accept*="image/png"]', "input[type=file]"):
            got = c.set_file(sel, str(path))
            if got.get("ok"):
                return {"ok": True, "selector": sel, "node": got["node"]}
        return {"ok": False, "why": "没找到图片 file input"}


def dialog_text() -> str:
    return e.js("(()=>{const d=window.__gb.dlg(); return d? window.__gb.t(d).slice(0,220):'';})()") or ""


def set_cover(aid: str, cover: Path, dry: bool = False) -> dict:
    if not cover.is_file():
        return {"stage": "no-file", "cover": str(cover)}
    e.launch()
    log(f"aid={aid} 封面={cover.name} ({cover.stat().st_size:,} B)")
    e.close_tab()
    e.new_tab(EDIT.format(aid=aid), wait=9)
    e.js(JS_HELPERS, await_promise=False)
    log(f"关杂弹窗：{close_extra_dialogs() or '无'}")

    if not open_dialog():
        return {"stage": "no-dialog"}
    if dry:
        return {"stage": "dry", "dialog": dialog_text()}

    got = set_file(cover)
    log(f"选文件：{got}")
    if not got.get("ok"):
        return {"stage": "file-failed", "got": got}

    for i in range(20):
        time.sleep(2)
        t = dialog_text()
        if t and "拖拽图片或点击上传" not in t:
            log(f"预览出现（{2 * i + 2}s）")
            break
    else:
        log(f"没等到预览，弹窗文字：{dialog_text()[:120]}")

    done = e.js("window.__gb.click('完成', window.__gb.dlg())")
    log(f"完成：{done}")
    time.sleep(3)

    sync = e.js("window.__gb.click('确认同步')")
    log(f"确认同步：{sync}")
    time.sleep(2)

    submit = e.js("window.__gb.click('提交') || window.__gb.click('立即投稿') || window.__gb.click('更新')")
    log(f"提交：{submit}")
    time.sleep(6)
    body = e.js("window.__gb.t(document.body).slice(0,100)")
    log(f"之后页面：{body}")
    e.close_tab()
    return {"stage": "done", "done": done, "sync": sync, "submit": submit}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aid")
    ap.add_argument("--cover")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    if args.all:
        aids = json.loads((ROOT / "out" / "aids.json").read_text(encoding="utf-8-sig"))
        plan = []
        for key, aid in aids.items():
            key = str(key)
            if not key.startswith("ep"):
                continue
            cov = ROOT / "out" / "covers_new" / f"{key}_cover.png"
            if cov.is_file():
                plan.append((key, str(aid), cov))
        log(f"批量：{len(plan)} 条")
        res = {}
        for key, aid, cov in plan:
            try:
                res[key] = set_cover(aid, cov, dry=args.dry).get("stage")
            except Exception as exc:  # 单条失败不拖垮整批
                res[key] = f"error: {exc}"
            log(f"—— {key} => {res[key]}")
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0

    if not args.aid or not args.cover:
        raise SystemExit("需要 --aid 与 --cover（或用 --all）")
    print(json.dumps(set_cover(args.aid, Path(args.cover), dry=args.dry), ensure_ascii=False)[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
