"""细看编辑页的封面模块：现有封面、可点的入口、弹窗里的文件框。只读。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edge_cdp as e  # noqa: E402

EDIT = "https://member.bilibili.com/platform/upload/video/frame?aid={aid}&type=edit"


def dump() -> dict:
    return e.js(
        r"""
(() => {
  const t = (n) => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
  const mod = document.querySelector('.cover-module');
  const imgs = mod ? [...mod.querySelectorAll('img')].map(i => ({src: i.src.slice(0, 90), w: i.width, h: i.height})) : [];
  const clickable = mod ? [...mod.querySelectorAll('div,button,span,a')]
      .filter(n => /封面上传|更换|修改|上传|完成|编辑/.test(t(n)) && t(n).length < 12)
      .map(n => ({cls: (n.className||'').toString().slice(0,40), txt: t(n)})) : [];
  const part = [...document.querySelectorAll('.select-controller, .bcc-select, [class*=partition]')]
      .map(n => ({cls: (n.className||'').toString().slice(0,40), txt: t(n).slice(0, 30)})).slice(0, 10);
  return {
    mod_text: mod ? t(mod).slice(0, 300) : '(没有 .cover-module)',
    imgs, clickable, part,
    dialogs: [...document.querySelectorAll('.bcc-dialog, .modal, [class*=dialog]')]
      .map(n => ({cls: (n.className||'').toString().slice(0, 40), txt: t(n).slice(0, 120)})).slice(0, 6),
  };
})()
"""
    )


def main() -> int:
    aid = sys.argv[1] if len(sys.argv) > 1 else "117660931066948"
    e.launch()
    e.nav(EDIT.format(aid=aid))
    time.sleep(6)
    print(json.dumps(dump(), ensure_ascii=False, indent=2)[:3500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
