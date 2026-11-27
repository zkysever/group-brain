#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""publish_cdp.py —— 用「持久登录的 Edge 窗口 + CDP」投稿。

为什么有这一版：
    publish_bsk.py 走 bsk 的 Agent Window，而那个窗口是**无痕** profile，
    关掉就掉登录，每次投稿都要人扫一次码（2026-11-22 为此折腾了一整晚）。
    tools/edge_cdp.py 起的是 --user-data-dir=C:\\Users\\Administrator\\.dsh\\edge-pub
    的常规窗口，登录会存盘 —— 这台机器上已经验证过：杀掉进程重启，仍是
    「原酱小窝 / 3546624852756939」。

它跟 publish_bsk.py 的关系：
    · 表单操作的 JS 基本照搬（那部分本来就是纯 DOM，不依赖 bsk）；
    · 三处必须换掉：js() → CDP Runtime.evaluate；等上传完成 → 轮询 DOM 文字；
      简介 → 焦点 + CDP Input.insertText（Quill 只认真正的输入事件）。

用法：
    python tools/publish_cdp.py --meta out/queue/ep08/meta.json \\
        --video out/ep08.mp4 --cover out/ep08_cover.png

    # 只填表不提交（排练）
    python tools/publish_cdp.py --meta ... --video ... --cover ... --dry
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import edge_cdp as e  # noqa: E402

UPLOAD_PAGE = "https://member.bilibili.com/platform/upload/video/frame"


def log(msg: str) -> None:
    print(f"[pub] {msg}", flush=True)


## ---------------------------------------------------------------- 基础动作

def js(expr: str, timeout: int = 40):
    """跑一段 JS，返回 JSON 解码后的对象（失败返回 None）。"""
    raw = e.js(expr, timeout=timeout)
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def shot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = e.call("Page.captureScreenshot", {"format": "png"})
        import base64
        path.write_bytes(base64.b64decode(data["data"]))
        log(f"  截图 {path.name}")
    except Exception as exc:  # 截图失败不影响发布
        log(f"  截图失败（忽略）：{exc}")


def click_text(text: str, tag: str = "button", tries: int = 20) -> bool:
    code = (
        "(async () => { const T = " + json.dumps(text, ensure_ascii=False) + ";"
        " const TAG = " + json.dumps(tag) + ";"
        " const sleep = ms => new Promise(r => setTimeout(r, ms));"
        " for (let i = 0; i < " + str(tries) + "; i++) {"
        "  const els = [...document.querySelectorAll(TAG + ',div,span,article,li')]"
        "    .filter(el => el.offsetParent !== null)"
        "    .filter(el => (el.innerText || '').trim() === T);"
        "  if (els.length) { const el = els[els.length - 1]; el.scrollIntoView({ block: 'center' });"
        "    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(tp =>"
        "      el.dispatchEvent(new MouseEvent(tp, { bubbles: true, cancelable: true, view: window })));"
        "    return JSON.stringify({ ok: true, tag: el.tagName }); }"
        "  await sleep(400); }"
        " return JSON.stringify({ ok: false }); })()"
    )
    out = js(code, timeout=tries * 2 + 20)
    ok = bool(isinstance(out, dict) and out.get("ok"))
    log(f"  点「{text}」→ {'ok' if ok else 'no'}")
    return ok


def page_text(limit: int = 4000) -> str:
    raw = e.js("document.body ? document.body.innerText.slice(0, %d) : ''" % limit)
    return raw or ""


## ---------------------------------------------------------------- 各步骤

def inject_video(path: Path) -> None:
    log(f"注入视频 {path.name}（{path.stat().st_size / 1048576:.2f} MB，走真实文件选择）")
    with e.session() as c:
        r = c.set_file("input[type=file][accept*='.mp4']", str(path))
    log(f"  set_file → {r}")


def wait_uploaded(limit_s: int = 480) -> None:
    """等上传完成。

    坑：页面在转码 / 提交表单时会短暂卡住主线程，CDP 的 Runtime.evaluate 会返回
    None（不是报错）。2026-11-27 第一次跑就是这么「超时」的——其实视频早就传完了。
    所以这里对 None 只当作「这一轮没读到」，继续等。
    """
    log("等上传完成")
    t0 = time.time()
    misses = 0
    while time.time() - t0 < limit_s:
        txt = ""
        for _ in range(3):
            got = e.js("document.body ? document.body.innerText : ''")
            if got:
                txt = got
                break
            misses += 1
            time.sleep(1.5)
        if "上传完成" in txt or "更换视频" in txt:
            log(f"  完成，用时 {int(time.time() - t0)}s")
            return
        time.sleep(5)
    raise RuntimeError(f"上传超时（读了 {misses} 次空响应）")


def set_title(title: str) -> int:
    code = (
        "(() => { const V = " + json.dumps(title, ensure_ascii=False) + ";"
        " const setNative = (el, v) => { const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
        " d.set.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true }));"
        " el.dispatchEvent(new Event('change', { bubbles: true })); };"
        " const t = [...document.querySelectorAll('input')].find(el => (el.placeholder || '').indexOf('稿件标题') >= 0);"
        " if (!t) return JSON.stringify({ ok: false });"
        " setNative(t, V); return JSON.stringify({ ok: true, len: t.value.length }); })()"
    )
    out = js(code)
    n = (out or {}).get("len", 0) if isinstance(out, dict) else 0
    log(f"  标题 {n} 字 → {title}")
    return n


def set_desc(text: str) -> int:
    """简介：先点进编辑器拿焦点，再用 CDP 的 Input.insertText 真打字。

    Quill 只监听真正的输入事件：直接写 innerHTML / __quill.setText() 会让计数涨、
    页面看得见，但提交时不带（EP01、EP02 就是这么发出去空简介的）。
    """
    got = js(
        "(() => { const q = document.querySelector('.ql-editor');"
        " if (!q) return JSON.stringify({ ok: false });"
        " q.scrollIntoView({ block: 'center' }); q.focus();"
        " const r = document.createRange(); r.selectNodeContents(q);"
        " const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);"
        " return JSON.stringify({ ok: true, before: (q.innerText || '').length }); })()"
    )
    if not isinstance(got, dict) or not got.get("ok"):
        log("  找不到简介编辑器")
        return 0
    e.call("Input.insertText", {"text": text})
    time.sleep(1.2)
    n = e.js(
        "(() => { const q = document.querySelector('.ql-editor');"
        " return q ? String((q.innerText || '').trim().length) : '-1'; })()"
    )
    try:
        n = int(str(n).strip().strip('"'))
    except Exception:
        n = -1
    log(f"  简介 {n} 字 / 目标 {len(text)} 字 → {'OK' if n >= max(10, int(len(text) * 0.8)) else '少了'}")
    return max(n, 0)


def clear_tags(limit: int = 16) -> None:
    """把标签区清空。

    一次性点所有 × 是不行的：B 站这批 chip 是 React 组件，批量派发 click 只有
    一个生效（2026-11-27 实测「清了 10 个还剩 8 个」）。所以改成「每次点第一个，
    点完读一遍列表」的循环，读空了才算清完。
    """
    code = """
(() => {
  const wrap = document.querySelector('.tag-pre-wrp');
  if (!wrap) return 'no wrap';
  const chips = [...wrap.querySelectorAll('div,span,li')]
    .filter(el => el.offsetParent !== null && (el.innerText || '').trim());
  if (!chips.length) return 'empty';
  const chip = chips[0];
  const x = chip.querySelector('.close, svg.close, i.close, [class*=close]');
  if (!x) return 'no close';
  const r = x.getBoundingClientRect();
  const o = {bubbles: true, cancelable: true, view: window,
             clientX: r.x + r.width / 2, clientY: r.y + r.height / 2};
  ['pointerover','pointerdown','mousedown','pointerup','mouseup','click']
    .forEach(t => x.dispatchEvent(new MouseEvent(t, o)));
  return 'clicked:' + (chip.innerText || '').trim();
})()
"""
    seen = []
    for _ in range(limit):
        now = e.js("(() => { const w = document.querySelector('.tag-pre-wrp');"
                   " return w ? w.innerText.replace(/\\s+/g, ' ').trim() : ''; })()") or ""
        if not str(now).strip():
            break
        r = e.js(code)
        seen.append(str(r))
        time.sleep(0.45)
    log(f"  清标签 → 删了 {len(seen)} 个")


def add_tags(tags: list[str]) -> None:
    code = (
        "(async () => { const TAGS = " + json.dumps(tags, ensure_ascii=False) + ";"
        " const sleep = ms => new Promise(r => setTimeout(r, ms));"
        " const inp = [...document.querySelectorAll('input')].find(el => (el.placeholder || '').indexOf('按回车键') >= 0);"
        " if (!inp) return JSON.stringify({ ok: false, reason: 'no tag input' });"
        " const setNative = (el, v) => { const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
        " d.set.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true })); };"
        " for (const t of TAGS) { inp.focus(); setNative(inp, t); await sleep(160);"
        " inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));"
        " await sleep(360); }"
        " const w = document.querySelector('.tag-pre-wrp');"
        " return JSON.stringify({ ok: true, now: w ? w.innerText.replace(/\\s+/g, ' ').trim() : '' }); })()"
    )
    out = js(code, timeout=90)
    log(f"  标签 → {out}")


def pick_partition(name: str) -> bool:
    """选分区。

    B 站这个下拉是两级自绘组件：先点 `.select-controller` 展开，
    再把 `.drop-list-v2-item` 里文字完全等于目标的那个点掉。
    之前按「页面上找 人工智能 文字」点，会点到「推荐标签」里的同名 chip。
    """
    log(f"选分区「{name}」")
    open_code = (
        "(() => { const el = document.querySelector('.select-controller, .selector-container .select-controller');"
        " if (!el) return 'no'; el.scrollIntoView({block:'center'});"
        " ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t =>"
        "  el.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window})));"
        " return 'ok'; })()"
    )
    if (e.js(open_code) or "") != "ok":
        log("  找不到分区下拉")
        return False
    time.sleep(1.2)

    pick_code = (
        "(() => { const T = " + json.dumps(name, ensure_ascii=False) + ";"
        " const items = [...document.querySelectorAll('.drop-list-v2-item')].filter(el => el.offsetParent !== null);"
        " let el = items.find(x => (x.innerText || '').trim() === T);"
        " if (!el) el = items.find(x => (x.innerText || '').trim().indexOf(T) >= 0);"
        " if (!el) return JSON.stringify({ok:false, n: items.length});"
        " const r = el.getBoundingClientRect();"
        " const o = {bubbles:true, cancelable:true, view:window, clientX: r.x + r.width/2, clientY: r.y + r.height/2};"
        " ['pointerover','pointerdown','mousedown','pointerup','mouseup','click'].forEach(t =>"
        "  el.dispatchEvent(new MouseEvent(t, o)));"
        " return JSON.stringify({ok:true}); })()"
    )
    out = js(pick_code)
    time.sleep(1.2)
    now = e.js("(() => { const el = document.querySelector('.select-controller'); return el ? (el.innerText||'').trim() : ''; })()")
    ok = (now or "").strip() == name
    log(f"  分区现在是「{now}」→ {'ok' if ok else '没选对'}")
    return ok


def pick_declaration(label: str) -> bool:
    """选创作声明：点那个 bcc-select 的输入框，再点选项列表里文字相同的项。"""
    log(f"选创作声明「{label}」")
    open_code = (
        "(() => { const el = [...document.querySelectorAll('input.bcc-select-input-inner')]"
        "  .find(x => (x.placeholder || '').indexOf('创作声明') >= 0);"
        " if (!el) return 'no'; el.scrollIntoView({block:'center'}); el.focus();"
        " ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t =>"
        "  (el.parentElement || el).dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window})));"
        " return 'ok'; })()"
    )
    if (e.js(open_code) or "") != "ok":
        log("  找不到创作声明下拉")
        return False
    time.sleep(1.2)

    pick_code = (
        "(() => { const T = " + json.dumps(label, ensure_ascii=False) + ";"
        " const items = [...document.querySelectorAll('.bcc-select-option, .bcc-select-option-list li, .bcc-select-option-list div')]"
        "  .filter(el => el.offsetParent !== null);"
        " let el = items.find(x => (x.innerText || '').trim() === T);"
        " if (!el) el = items.find(x => (x.innerText || '').trim().indexOf(T) >= 0);"
        " if (!el) return JSON.stringify({ok:false, n: items.length});"
        " const r = el.getBoundingClientRect();"
        " const o = {bubbles:true, cancelable:true, view:window, clientX: r.x + r.width/2, clientY: r.y + r.height/2};"
        " ['pointerover','pointerdown','mousedown','pointerup','mouseup','click'].forEach(t =>"
        "  el.dispatchEvent(new MouseEvent(t, o)));"
        " return JSON.stringify({ok:true}); })()"
    )
    out = js(pick_code)
    time.sleep(1.0)
    now = e.js("(() => { const el = [...document.querySelectorAll('input.bcc-select-input-inner')]"
               ".find(x => (x.placeholder || '').indexOf('创作声明') >= 0); return el ? el.value : ''; })()")
    ok = (now or "") == label
    log(f"  声明现在是「{now}」→ {'ok' if ok else '没选对'}")
    return ok


def set_cover(path: Path) -> None:
    log("上传封面")
    if not click_text("添加封面", "span", tries=6):
        click_text("添加封面", "button", tries=6)
    time.sleep(3)
    with e.session() as c:
        r = c.set_file("input[type=file][accept*='image/png']", str(path))
    log(f"  set_file → {r}")
    time.sleep(5)
    shot(ROOT / "out" / "publish_cover_dialog.png")
    click_text("完成", "div", tries=6)
    time.sleep(3)
    # B 站偶尔会弹「确认同步」
    if "确认同步" in page_text():
        click_text("确认同步", "div", tries=4)
        time.sleep(2)
        click_text("完成", "div", tries=4)


def submit(dry: bool = False) -> bool:
    if dry:
        log("--dry：不点投稿")
        return False
    log("点「立即投稿」")
    ok = click_text("立即投稿", "span", tries=10) or click_text("立即投稿", "button", tries=10)
    time.sleep(6)
    txt = page_text(800)
    hit = ("投稿成功" in txt) or ("稿件投递成功" in txt) or ("审核" in txt)
    log(f"  结果 → {'成功' if hit else '未确认'}（页面开头：{txt[:60].replace(chr(10), ' ')}）")
    return hit


## ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--cover", default="")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()

    meta = json.loads(Path(args.meta).read_text(encoding="utf-8-sig"))
    video = Path(args.video)
    cover = Path(args.cover) if args.cover else None
    for p in [video] + ([cover] if cover else []):
        if not p.exists():
            raise SystemExit(f"找不到文件：{p}")

    me = e.whoami()
    log(f"当前登录：{me}")
    if not me.get("isLogin"):
        raise SystemExit("这个窗口没登录，先扫码（tools/edge_cdp.py launch 后手动登录一次）")

    e.nav(UPLOAD_PAGE, wait=6)
    time.sleep(2)
    shot(ROOT / "out" / "publish_1_upload.png")

    inject_video(video)
    wait_uploaded()
    shot(ROOT / "out" / "publish_2_uploaded.png")

    set_title(meta["title"])
    set_desc(meta.get("desc", ""))
    if meta.get("partition"):
        pick_partition(meta["partition"])
        time.sleep(1.0)
    for label in meta.get("declarations", []):
        pick_declaration(label)
        time.sleep(0.8)
    ## 分区一改，B 站会自动补「影视剪辑 / 爱情」这类标签，所以清标签必须放在分区之后
    clear_tags()
    add_tags(meta.get("tags", []))
    if cover:
        set_cover(cover)

    shot(ROOT / "out" / "publish_3_form.png")
    ok = submit(dry=args.dry)
    shot(ROOT / "out" / "publish_4_result.png")

    if args.keep_open:
        log("--keep-open：窗口留着")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
