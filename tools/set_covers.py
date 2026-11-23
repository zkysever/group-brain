"""批量换封面：只改合集（ep01–ep07），走一条长 CDP 连接做完一整套动作。

之前踩的坑（都在这儿解决了）：
  1. DOM 域状态是按连接记的 → DOM.getDocument / requestNode / setFileInputFiles
     必须在**同一条** websocket 上，分开调 requestNode 直接返回 null；
  2. 编辑页开过封面编辑器后整个标签页会卡死 → 每条视频开一个新标签页，做完关掉；
  3. 「完成」是 `div.button.submit`，不是 <button>，按标签找永远找不到。

用法：
    python tools/set_covers.py                # 全部合集
    python tools/set_covers.py ep07 ep06      # 指定几期
    python tools/set_covers.py --check        # 只核对线上封面
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edge_cdp as e  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EDIT = "https://member.bilibili.com/platform/upload/video/frame?aid={aid}&type=edit"
COVERS = ROOT / "out" / "covers_new"
AIDS = ROOT / "out" / "aids.json"
CK = Path(r"C:\Users\Administrator\.dsh\secrets\bili-cookies.json")
SERIES = ["ep01", "ep02", "ep03", "ep04", "ep05", "ep06", "ep07"]

JS = r"""
window.__gb = {
  t: (n) => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim(),
  tc: (n) => (n.innerText || n.textContent || '').replace(/\s+/g, ''),   // 去掉所有空白再比，防「完 成」
  vis: (n) => !!(n.offsetParent || n.getClientRects().length),
  // 同一层会同时存在「空的壳」和「真内容」两个都含『封面制作』的节点，
  // 取文本最长的那个才是真的编辑器（取最后一个会拿到空壳，预览判断就失真）。
  dlg: () => [...document.querySelectorAll('.bcc-dialog, [class*=dialog]')]
             .filter(n => __gb.vis(n) && /封面制作/.test(n.innerText || ''))
             .sort((a, b) => (b.innerText || '').length - (a.innerText || '').length)[0],
  find: (txt, scope) => {
    const root = scope || document;
    const want = txt.replace(/\s+/g, '');
    const cands = [...root.querySelectorAll('*')].filter(n => __gb.vis(n) && __gb.tc(n) === want);
    const leaf = cands.filter(n => ![...n.children].some(c => __gb.tc(c) === want));
    return leaf[0] || cands[cands.length - 1] || null;
  },
  click: (txt, scope) => { const n = __gb.find(txt, scope); if (!n) return {ok: false, want: txt}; n.click(); return {ok: true, txt}; },
  done: () => {
    const n = __gb.find('完成');           // 弹窗里的「完成」在弹窗节点外，只能全页找
    if (!n) return {ok: false, want: '完成'};
    n.click();
    return {ok: true, cls: (n.className || '').toString().slice(0, 40)};
  },
};
'ok';
"""


def log(m: str) -> None:
    print(f"[covers] {m}", flush=True)


def api_archives() -> dict:
    ck = json.loads(CK.read_text(encoding="utf-8"))
    hdr = {
        "Cookie": "; ".join(f"{k}={v}" for k, v in ck.items()),
        "User-Agent": "Mozilla/5.0 Chrome/158.0 Safari/537.36",
        "Referer": "https://member.bilibili.com/platform/upload-manager/article",
    }
    url = ("https://member.bilibili.com/x/web/archives"
           "?status=is_pubing,pubed,not_pubed,is_reject&pn=1&ps=50")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    d = json.loads(op.open(urllib.request.Request(url, headers=hdr), timeout=25).read().decode())
    out = {}
    for a in ((d.get("data") or {}).get("arc_audits") or []):
        arc = a.get("Archive") or {}
        out[str(arc.get("aid"))] = {
            "bvid": arc.get("bvid"),
            "title": arc.get("title"),
            "cover": arc.get("cover"),
            "state": arc.get("state"),
        }
    return out


def wait_for(c, expr: str, seconds: float, step: float = 1.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if c.js(expr):
            return True
        time.sleep(step)
    return False


def one(key: str, aid: str, cover: Path) -> str:
    log(f"=== {key} aid={aid} 封面 {cover.name} {cover.stat().st_size:,}B")
    e.close_tab()
    e.new_tab(EDIT.format(aid=aid), wait=1.0)
    try:
        with e.session(timeout=30) as c:
            if not wait_for(c, "!!document.querySelector('.cover-img')", 40):
                return "no-cover-tile"
            c.js(JS)
            c.js("(()=>{const n=document.querySelector('.cover-img'); if(n) n.click(); return !!n;})()")
            if not wait_for(c, "!!(__gb.dlg() && __gb.dlg())", 20):
                return "no-dialog"
            log("  弹窗开了")
            got = c.set_file('input[type=file][accept*="image/png"]', str(cover))
            log(f"  选文件 {got}")
            if not got.get("ok"):
                return "file-failed"
            ok = wait_for(c, "(()=>{const d=__gb.dlg();"
                             "return !!d && !/拖拽图片或点击上传/.test(d.innerText||'');})()", 45)
            log(f"  预览 {'来了' if ok else '没来'}")
            # 预览出来之后「完成」才会渲染成可点状态，先等它出现
            if not wait_for(c, "!!__gb.find('完成')", 25, 0.5):
                return "no-done-btn"
            time.sleep(1)
            # 勾上「双比例同步改动」，否则只改 4:3、16:9 还是老的
            sync2 = c.js("__gb.click('双比例同步改动')")
            log(f"  双比例同步 {sync2}")
            time.sleep(2)
            r = c.js("__gb.done()")
            log(f"  完成 {r}")
            log(f"  弹窗{'关了' if wait_for(c, '!__gb.dlg()', 20, 0.5) else '没有关'}")
            time.sleep(2)
            s = c.js("__gb.click('确认同步')")
            log(f"  确认同步 {s}")
            time.sleep(2)
            # 提交按钮改完封面才可用，等它出现再点
            sub = None
            for _ in range(20):
                sub = c.js("__gb.click('立即投稿') || __gb.click('提交') || __gb.click('保存')")
                if (sub or {}).get("ok"):
                    break
                time.sleep(1)
            log(f"  提交 {sub}")
            time.sleep(7)
            body = c.js("__gb.t(document.body).slice(0,80)")
            log(f"  之后 {body}")
            return "ok" if (sub or {}).get("ok") else "submit-missed"
    finally:
        e.close_tab()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    # aids.json 是全量稿件列表，真正的对应关系在 out/queue/state.json（id → bvid）
    raw = json.loads(AIDS.read_text(encoding="utf-8-sig"))
    by_bvid = {str(x.get("bvid")): str(x.get("aid")) for x in (raw if isinstance(raw, list) else [])}
    state = json.loads((ROOT / "out" / "queue" / "state.json").read_text(encoding="utf-8-sig"))
    aids = {k: by_bvid.get(v, "") for k, v in (state.get("published") or {}).items()}
    keys = args.keys or SERIES

    if args.check:
        cur = api_archives()
        res = {}
        for k in keys:
            aid = str(aids.get(k, ""))
            res[k] = (cur.get(aid) or {}).get("cover", "?")
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0

    before = api_archives()
    e.launch()
    out = {}
    for k in keys:
        aid = str(aids.get(k) or "")
        cov = COVERS / f"{k}_cover.png"
        if not aid or not cov.is_file():
            out[k] = "skip(no aid/cover)"
            continue
        try:
            out[k] = one(k, aid, cov)
        except Exception as exc:
            out[k] = f"error: {type(exc).__name__}: {exc}"
        log(f"--- {k} => {out[k]}")

    time.sleep(4)
    after = api_archives()
    changed = {}
    for k in keys:
        aid = str(aids.get(k) or "")
        b = (before.get(aid) or {}).get("cover")
        a = (after.get(aid) or {}).get("cover")
        changed[k] = "changed" if (a and a != b) else "same"
    print(json.dumps({"steps": out, "covers": changed}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
