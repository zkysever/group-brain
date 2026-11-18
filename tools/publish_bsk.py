"""把「投稿到 B 站」这件事写成脚本，供每天定时任务调用。

走过的弯路（都写在注释里，免得下次再撞）：

1. **登录只能扫码**。把服务端存的 SESSDATA 用 `document.cookie` 写进浏览器是**无效**的：
   B 站把它设成 HttpOnly，Chrome 会忽略同名的 JS 写入（同一份 cookie 用纯 HTTP 请求
   member API 却是有效的，说明 cookie 没过期，卡的是浏览器写不进去）。
   所以流程是：脚本把登录页打开 → 截图给用户扫 → 用户说「扫了」→ 再跑一次（带 `--skip-login`）。
2. **文件不能走 bsk upload**。`input[type=file]` 页面上有三个，`--selector` 会报
   `selector matched 3 elements`；上传按钮上有 `pointer-events: none` 也点不动。
   所以走页面内 `fetch`（带 OUT_TOKEN 的 /out/ 直链）→ `DataTransfer` → `change` 事件。
3. **一次大操作会卡住会话**。注入 8MB 时 `bsk evaluate` 会超时并让会话变 frozen，
   所以 evaluate 一律 `--timeout 120s`；真卡了用 `bsk ledger resolve --operation-id …`。
4. **创作声明、分区是自定义下拉**（`bcc-select-input-inner`），必须先点开输入框，
   等选项渲染出来，再去点 `button "<选项文字>"`——直接点按钮是点不到的。
5. **封面上传后会弹「检测到个人空间封面（16:9）未修改」**，要点「确认同步」再点「完成」，
   只点一次「完成」会停在弹窗里，封面等于没设。

用法（在 group-brain 目录下）：
    # 第一步：打开登录页，让用户扫码
    python tools/publish_bsk.py --login-only
    # 第二步：用户扫完码
    python tools/publish_bsk.py --session <id> --skip-login \
        --meta out/queue/ep03/meta.json \
        --video-url https://zkysever.com/out/ep03.mp4?k=… \
        --cover-url https://zkysever.com/out/ep03_cover.png?k=…

`--dry` 只填表不点「立即投稿」，用于彩排。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BSK = Path(r"C:\Users\Administrator\.local\bin\bsk.exe")
SCOPE = ""
SITE = "https://www.bilibili.com/"
LOGIN_PAGE = "https://passport.bilibili.com/login"
UPLOAD_PAGE = "https://member.bilibili.com/platform/upload/video/frame"
MID_UPLOAD = "3546624852756939"
DESC_HINT = "填写更全面的相关信息"
EDIT_URL = "https://member.bilibili.com/platform/upload/video/frame?aid={aid}&type=edit"


def log(msg: str) -> None:
    print(f"[publish] {msg}", flush=True)


def bsk(*args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(BSK), *args]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"bsk {' '.join(args)} 失败: {(p.stderr or p.stdout).strip()[:400]}")
    return p


def js(code: str, timeout: str = "120s") -> str:
    """执行一段 JS。用 base64 传参，绕开 PowerShell/cmd 对引号与空格的破坏。"""
    expr = "eval(decodeURIComponent(escape(atob('" + base64.b64encode(code.encode()).decode() + "'))))"
    p = bsk("evaluate", "--session", SCOPE, "--unverified", "--timeout", timeout, expr, check=False)
    out = (p.stdout or "").strip()
    if p.returncode != 0 and "ran unverified" not in (p.stderr or ""):
        raise RuntimeError(f"evaluate 失败: {(p.stderr or out).strip()[:300]}")
    return out


def sync() -> dict:
    p = bsk("sync", "--session", SCOPE, "--json", timeout=90)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return json.loads(p.stdout[p.stdout.find("{"):])


def ensure_session(want: str = "") -> str:
    """返回可用的 session_id。

    `bsk session start --name X` 的 name 不会出现在 `session list` 里，也**不能**当
    `--session` 用（会报 session not registered），所以只能从返回值里取 session_id。
    """
    global SCOPE
    if want:
        p = bsk("sync", "--session", want, "--json", check=False, timeout=60)
        if p.returncode == 0:
            SCOPE = want
            log("复用会话 " + SCOPE)
            return SCOPE
    p = bsk("session", "start", "--json", check=False, timeout=90)
    sid = ""
    try:
        sid = (json.loads(p.stdout) or {}).get("session_id") or ""
    except Exception:
        m = re.search(r'"session_id":\s*"(\w+)"', p.stdout or "")
        sid = m.group(1) if m else ""
    if not sid:
        raise RuntimeError(f"建会话失败: {(p.stderr or p.stdout).strip()[:300]}")
    SCOPE = sid
    log("建会话 " + SCOPE)
    return SCOPE


def open_url(url: str) -> None:
    """打开地址。空白标签页（about:）时 `sync` 会报 `Page frames cannot be observed`，
    那时 `open` **不能**带 `--observation`；页面有内容后才必须带。"""
    obs = ""
    try:
        obs = (sync() or {}).get("observation_id") or ""
    except Exception:
        obs = ""
    args = ["open", url, "--session", SCOPE, "--focus"]
    if obs:
        args += ["--observation", obs]
    bsk(*args, check=False)
    time.sleep(1.5)


def shot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bsk("screenshot", "--session", SCOPE, "--out", str(path), check=False)


def wait_login(limit_s: int = 420) -> None:
    """等真正登录（判据用 VOM 里的投稿号 mid，不用页面 url：upload 页的 url 常为空）。"""
    log("等登录…")
    t0 = time.time()
    while time.time() - t0 < limit_s:
        try:
            st = sync()
        except Exception:
            time.sleep(3)
            continue
        vom = st.get("vom") or ""
        if MID_UPLOAD in vom or "上传视频" in vom or "投稿" in vom and "登录" not in vom:
            log(f"  已登录（{int(time.time() - t0)}s）")
            return
        time.sleep(3)
    raise RuntimeError("等登录超时")


def login_only() -> int:
    ensure_session()
    open_url(LOGIN_PAGE)
    time.sleep(2)
    shot(Path("out") / "login_qr.png")
    log("登录页已打开，截图在 out/login_qr.png；让用户扫码后加 --skip-login 再跑一次")
    return 0


def upload_video(out_url: str, filename: str) -> str:
    log("注入视频（页面内 fetch + DataTransfer）")
    code = (
        "(async () => {"
        f" const r = await fetch({json.dumps(out_url)}, {{ cache: 'no-store' }});"
        " if (!r.ok) return JSON.stringify({ ok: false, status: r.status });"
        " const b = await r.blob();"
        " const ins = [...document.querySelectorAll('input[type=file]')];"
        " const t = ins.find(e => ((e.accept || '').indexOf('.mp4') >= 0));"
        " if (!t) return JSON.stringify({ ok: false, reason: 'no video input' });"
        " const dt = new DataTransfer();"
        f" dt.items.add(new File([b], {json.dumps(filename)}, {{ type: 'video/mp4' }}));"
        " t.files = dt.files; t.dispatchEvent(new Event('change', { bubbles: true }));"
        " return JSON.stringify({ ok: true, bytes: b.size }); })()"
    )
    return js(code, timeout="180s")


def wait_uploaded(limit_s: int = 300) -> None:
    log("等上传完成")
    t0 = time.time()
    while time.time() - t0 < limit_s:
        if "上传完成" in (sync().get("vom") or ""):
            log(f"  完成，用时 {int(time.time() - t0)}s")
            return
        time.sleep(5)
    raise RuntimeError("上传超时（可能网络慢，或页面被风控弹窗挡住）")


def fill_text(meta: dict) -> str:
    code = (
        "(() => { const TITLE = " + json.dumps(meta["title"], ensure_ascii=False) + ";"
        " const out = {};"
        " const setNative = (el, v) => { const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
        " d.set.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true }));"
        " el.dispatchEvent(new Event('change', { bubbles: true })); };"
        " const t = [...document.querySelectorAll('input')].find(e => (e.placeholder || '').indexOf('稿件标题') >= 0);"
        " if (t) { setNative(t, TITLE); out.title = t.value.length; }"
        " return JSON.stringify(out); })()"
    )
    return js(code)


def find_ref(needle: str) -> str:
    """在 VOM 里按文字找 @eN（填简介这类富文本只能用 bsk fill 走真输入）。"""
    vom = sync().get("vom") or ""
    for line in vom.splitlines():
        if needle in line and "@e" in line:
            return "@e" + line.split("@e")[1].split()[0]
    return ""


def fill_desc(text: str) -> bool:
    """填简介。

    **不能用 JS 写 Quill**：`__quill.setText()` 页面上看得见、计数也会涨，
    但提交时不带这段内容（EP01/EP02 第一次发布简介就是空的）。
    只有 `bsk fill` 往编辑器里真打字，框架状态才会更新。
    """
    ref = ""
    for _ in range(6):
        ref = find_ref(DESC_HINT)
        if ref:
            break
        time.sleep(2)
    if not ref:
        log("  没找到简介编辑器（placeholder 不在：可能已经有内容了）")
        return False
    p = bsk("fill", "--session", SCOPE, "--ref", ref, "--value", text,
            "--timeout", "120s", check=False)
    if p.returncode != 0:
        log("  简介填充失败：" + (p.stderr or p.stdout).strip()[:200])
        return False
    time.sleep(1)
    # 校验用「编辑器里实际有多少字」，不能用 placeholder 是否还在：
    # 填成功后 placeholder 本来就会消失，2026-11-18 那次就是被这条判据误报成失败。
    r = js(
        "(() => { const q = document.querySelector('.ql-editor');"
        " return q ? String((q.innerText || '').trim().length) : '-1'; })()"
    )
    raw = (r or "").strip()
    if raw.startswith('{'):
        try:
            obj = json.loads(raw)
            raw = str(obj.get("value", obj.get("result", "")))
        except json.JSONDecodeError:
            pass
    try:
        n = int(raw.strip().strip('"').strip())
    except ValueError:
        n = -1
    ok = n >= max(10, int(len(text) * 0.8))
    log(f"  简介填入 {n} 字 / 目标 {len(text)} 字 → {'OK' if ok else '少了'}")
    return ok



def clear_tags() -> str:
    """清掉 B 站按分区自动塞进来的标签（影视分区会给 REACTION / 爱情 / 电视剧）。"""
    code = (
        "(() => { const wrap = document.querySelector('.tag-pre-wrp') || document.body;"
        " const closes = [...wrap.querySelectorAll('svg.close, .close')];"
        " closes.forEach(c => { const b = c.closest('div,span'); (b || c).dispatchEvent(new MouseEvent('click', { bubbles: true })); });"
        " return JSON.stringify({ cleared: closes.length }); })()"
    )
    return js(code)


def add_tags(tags: list[str]) -> str:
    code = (
        "(async () => { const TAGS = " + json.dumps(tags, ensure_ascii=False) + ";"
        " const sleep = ms => new Promise(r => setTimeout(r, ms));"
        " const inp = [...document.querySelectorAll('input')].find(e => (e.placeholder || '').indexOf('按回车键') >= 0);"
        " if (!inp) return JSON.stringify({ ok: false, reason: 'no tag input' });"
        " const setNative = (el, v) => { const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
        " d.set.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true })); };"
        " const done = [];"
        " for (const t of TAGS) { inp.focus(); setNative(inp, t); await sleep(140);"
        " inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));"
        " await sleep(320); done.push(t); }"
        " const w = document.querySelector('.tag-pre-wrp');"
        " return JSON.stringify({ ok: true, added: done, now: w ? w.innerText.replace(/\\s+/g, ' ').trim() : '' }); })()"
    )
    return js(code)


def click_text(text: str, tag: str = "button", timeout_s: float = 8.0) -> bool:
    """在页面上按可见文字找元素并点它（点的是真 DOM，不依赖 bsk 的 ref 编号）。"""
    code = (
        "(async () => { const T = " + json.dumps(text, ensure_ascii=False) + ";"
        " const TAG = " + json.dumps(tag) + ";"
        " const sleep = ms => new Promise(r => setTimeout(r, ms));"
        " for (let i = 0; i < 20; i++) {"
        "  const els = [...document.querySelectorAll(TAG + ',div,span,article,li')]"
        "    .filter(e => e.offsetParent !== null)"
        "    .filter(e => (e.innerText || '').trim() === T);"
        "  if (els.length) { const el = els[els.length - 1]; el.scrollIntoView({ block: 'center' });"
        "    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(tp =>"
        "      el.dispatchEvent(new MouseEvent(tp, { bubbles: true, cancelable: true, view: window })));"
        "    return JSON.stringify({ ok: true, n: els.length, tag: el.tagName }); }"
        "  await sleep(400); }"
        " return JSON.stringify({ ok: false }); })()"
    )
    out = js(code, timeout=f"{int(timeout_s) + 15}s")
    log(f"  点「{text}」→ {out}")
    try:
        return bool(json.loads(out).get("ok"))
    except Exception:
        return False


def open_select(label_placeholder: str) -> str:
    """点开自定义下拉（bcc-select）。placeholder 是「请选择…」那段文字。"""
    code = (
        "(() => { const P = " + json.dumps(label_placeholder, ensure_ascii=False) + ";"
        " const els = [...document.querySelectorAll('input,div,span')]"
        "   .filter(e => e.offsetParent !== null)"
        "   .filter(e => { const t = (e.innerText || '').trim(); const ph = (e.getAttribute && e.getAttribute('placeholder')) || '';"
        "     return t.indexOf(P) >= 0 || ph.indexOf(P) >= 0 || (e.value || '').indexOf(P) >= 0; });"
        " if (!els.length) return JSON.stringify({ ok: false });"
        " const el = els[els.length - 1]; el.scrollIntoView({ block: 'center' });"
        " ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(tp =>"
        "   el.dispatchEvent(new MouseEvent(tp, { bubbles: true, cancelable: true, view: window })));"
        " return JSON.stringify({ ok: true, tag: el.tagName, cls: (el.className || '').toString().slice(0, 40) }); })()"
    )
    return js(code, timeout="30s")


def pick_partition(name: str) -> bool:
    log(f"选分区「{name}」")
    open_select("分区")
    time.sleep(1.2)
    open_select(name if name else "选择分区")
    time.sleep(1.5)
    return click_text(name)


def pick_declaration(label: str) -> bool:
    log(f"选创作声明「{label}」")
    open_select("请选择符合您视频内容")
    time.sleep(1.2)
    return click_text(label)


def set_cover(cover_url: str) -> None:
    log("上传封面（封面制作弹窗）")
    if not click_text("添加封面", "span"):
        click_text("添加封面", "button")
    time.sleep(3)
    code = (
        "(async () => { const r = await fetch(" + json.dumps(cover_url) + ", { cache: 'no-store' });"
        " const b = await r.blob();"
        " const ins = [...document.querySelectorAll('input[type=file]')];"
        " const t = ins.find(e => ((e.accept || '').indexOf('image/png') >= 0));"
        " if (!t) return JSON.stringify({ ok: false });"
        " const dt = new DataTransfer();"
        " dt.items.add(new File([b], 'cover.png', { type: 'image/png' }));"
        " t.files = dt.files; t.dispatchEvent(new Event('change', { bubbles: true }));"
        " return JSON.stringify({ ok: true, bytes: b.size }); })()"
    )
    log("  " + js(code))
    time.sleep(6)
    shot(Path("out") / "cover_dialog.png")
    # 弹窗顺序：完成 → （可能弹）确认同步 → 完成
    click_text("完成")
    time.sleep(2.5)
    if click_text("确认同步"):
        time.sleep(2)
        click_text("完成")
    time.sleep(2.5)
    # 弹窗没关掉就再点一次（第二次失败常见于按钮文字带空格）
    if "封面制作" in (sync().get("vom") or ""):
        log("  弹窗还在，再点一次「完成」")
        click_text("完成")
        time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta")
    ap.add_argument("--video-url")
    ap.add_argument("--cover-url", default="")
    ap.add_argument("--session", default="")
    ap.add_argument("--skip-login", action="store_true")
    ap.add_argument("--login-only", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--fix-desc", action="append", default=[],
                    metavar="AID=FILE", help="发完之后顺手补某个旧稿件的简介")
    args = ap.parse_args()

    if args.login_only:
        return login_only()

    for need in ("meta", "video_url"):
        if not getattr(args, need):
            raise SystemExit(f"缺 --{need.replace('_', '-')}")

    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    ensure_session(args.session)
    if not args.skip_login:
        wait_login()
    open_url(UPLOAD_PAGE)
    time.sleep(3)

    log("注入视频 → " + upload_video(args.video_url, meta.get("filename", "episode.mp4")))
    wait_uploaded()
    log("填标题 → " + fill_text(meta))
    if not fill_desc(meta["desc"]):
        log("  !! 简介没填上，提交后要补")
    log("清旧标签 → " + clear_tags())
    time.sleep(1)
    log("加标签 → " + add_tags(meta.get("tags") or []))

    part = (meta.get("partition") or "").strip()
    if part:
        pick_partition(part)
    for label in meta.get("declarations") or []:
        pick_declaration(label)
        time.sleep(1)

    if args.cover_url:
        set_cover(args.cover_url)

    shot(Path("out") / "publish_before_submit.png")
    if args.dry:
        log("dry-run：停在提交前")
        return 0

    log("点「立即投稿」")
    if not click_text("立即投稿"):
        raise RuntimeError("没找到「立即投稿」按钮（可能还有必填项没填）")
    time.sleep(10)
    shot(Path("out") / "publish_done.png")
    vom = sync().get("vom") or ""
    if "投递成功" not in vom:
        raise RuntimeError("点完投稿没看到「投递成功」，看 out/publish_done.png")
    log("投稿成功")

    # 顺手补旧稿的简介（EP01/EP02 第一次发布时简介被 JS 写丢了）
    for spec in args.fix_desc or []:
        aid, _, path = spec.partition("=")
        if not aid or not path:
            log(f"  --fix-desc 格式不对，跳过：{spec}")
            continue
        fix_desc(aid.strip(), Path(path))
    return 0


def fix_desc(aid: str, path: Path) -> None:
    """打开编辑页，用 bsk fill 重填简介并提交。"""
    log(f"补简介 aid={aid}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        log("  文件是空的，跳过")
        return
    open_url(EDIT_URL.format(aid=aid))
    time.sleep(6)
    if not fill_desc(text):
        log("  填充失败")
        return
    shot(Path("out") / f"fixdesc_{aid}.png")
    if not click_text("立即投稿"):
        log("  没找到提交按钮")
        return
    time.sleep(10)
    vom = sync().get("vom") or ""
    log("  " + ("补完了" if "投递成功" in vom else "提交后没看到成功页，看截图"))



if __name__ == "__main__":
    sys.exit(main())
