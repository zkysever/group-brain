"""探一探 B 站「编辑稿件」页的封面区长什么样（只读，不改任何东西）。

用法：python tools/probe_edit.py <aid>
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edge_cdp as e  # noqa: E402

EDIT = "https://member.bilibili.com/platform/upload/video/frame?aid={aid}&type=edit"


def probe(aid: str) -> dict:
    e.launch()
    e.nav(EDIT.format(aid=aid))
    time.sleep(6)
    out = e.js(
        """
(() => {
  const txt = (el) => (el.innerText || el.textContent || '').trim().slice(0, 40);
  const inputs = [...document.querySelectorAll('input[type=file]')].map(i => ({
    accept: i.accept, name: i.name, cls: i.className, hidden: i.offsetParent === null,
  }));
  const btns = [...document.querySelectorAll('button, .bcc-button, [class*=btn]')]
    .map(b => txt(b)).filter(t => t && t.length < 20).slice(0, 60);
  const coverish = [...document.querySelectorAll('[class*=cover], [class*=Cover]')]
    .map(n => ({ cls: (n.className || '').toString().slice(0, 60), txt: txt(n).slice(0, 20) }))
    .slice(0, 20);
  return {
    href: location.href,
    title: document.title,
    body_head: (document.body ? document.body.innerText.replace(/\\s+/g, ' ').slice(0, 200) : ''),
    inputs, buttons: [...new Set(btns)].slice(0, 40), coverish,
  };
})()
"""
    )
    return out or {}


def main() -> int:
    aid = sys.argv[1] if len(sys.argv) > 1 else "117660931066948"
    info = probe(aid)
    print(json.dumps(info, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
