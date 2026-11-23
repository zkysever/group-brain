"""把封面区整块 HTML 打出来，弄清上传入口到底藏在哪。只读。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edge_cdp as e  # noqa: E402

EDIT = "https://member.bilibili.com/platform/upload/video/frame?aid={aid}&type=edit"

JS = r"""
(() => {
  const t = (n) => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
  const mod = document.querySelector('.cover-module');
  const html = mod ? mod.outerHTML.replace(/\s+/g, ' ').slice(0, 1800) : '(none)';
  const nodes = [...document.querySelectorAll('[class*=cover]')].slice(0, 25).map(n => {
    const r = n.getBoundingClientRect();
    return {
      tag: n.tagName, cls: (n.className || '').toString().slice(0, 50),
      txt: t(n).slice(0, 24), w: Math.round(r.width), h: Math.round(r.height),
      vis: !!(n.offsetParent || n.getClientRects().length),
    };
  });
  return {html, nodes};
})()
"""


def main() -> int:
    aid = sys.argv[1] if len(sys.argv) > 1 else "117660931066948"
    e.launch()
    e.nav(EDIT.format(aid=aid), wait=7)
    # 先把首页可能弹的二创弹窗按掉
    e.js(
        r"""
(() => {
  const wanted = ['暂不考虑', '取消', '知道了', '关闭'];
  for (const dlg of [...document.querySelectorAll('.bcc-dialog, [class*=dialog]')]) {
    if (!dlg.getClientRects().length) continue;
    const bs = [...dlg.querySelectorAll('button, [class*=btn]')];
    for (const w of wanted) {
      const hit = bs.find(b => (b.innerText || '').includes(w));
      if (hit) { hit.click(); break; }
    }
  }
  return 'ok';
})()
""",
        await_promise=False,
    )
    time.sleep(2)
    out = e.js(JS)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:4500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
