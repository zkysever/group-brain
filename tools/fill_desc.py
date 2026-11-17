"""改已发布稿件的「简介」。

为什么要单独一个脚本：简介是 Quill 富文本，用 JS 写 `__quill.setText()` 页面上看得见、
**但提交时不会带上**（EP01/EP02 第一次发布时简介就是空的，373 字的教训）。
唯一可靠的办法是 `bsk fill --ref` 往编辑器里真打字——它走输入事件，框架状态会跟着更新。

用法：
    python tools/fill_desc.py --aid 117626638435953 --desc-file desc.txt
    python tools/fill_desc.py --aid 117626638435953 --desc-file desc.txt --dry
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BSK = Path.home() / ".local" / "bin" / "bsk.exe"
EDIT_URL = "https://member.bilibili.com/platform/upload/video/frame?aid={aid}&type=edit"
DESC_HINT = "填写更全面的相关信息"


def log(msg: str) -> None:
    print(f"[desc] {msg}", flush=True)


def bsk(*args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run([str(BSK), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"bsk {' '.join(args[:2])} 失败: {(p.stderr or p.stdout).strip()[:300]}")
    return p


def sync(sess: str) -> dict:
    p = bsk("sync", "--session", sess, "--json", timeout=90)
    return json.loads(p.stdout[p.stdout.find("{"):])


def find_ref(vom: str, needle: str) -> str:
    for line in vom.splitlines():
        if needle in line and "@e" in line:
            return "@e" + line.split("@e")[1].split()[0]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aid", required=True)
    ap.add_argument("--desc-file", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    desc = Path(args.desc_file).read_text(encoding="utf-8").strip()
    if not desc:
        raise SystemExit("简介文件是空的")

    st = sync(args.session)
    obs = st.get("observation_id") or ""
    bsk("open", EDIT_URL.format(aid=args.aid), "--session", args.session,
        "--observation", obs, "--focus", check=False)
    time.sleep(8)

    ref = ""
    for _ in range(6):
        st = sync(args.session)
        ref = find_ref(st.get("vom") or "", DESC_HINT)
        if ref:
            break
        time.sleep(3)
    if not ref:
        raise RuntimeError("没找到简介编辑器（页面还没加载好？）")
    log(f"简介编辑器 {ref}")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(desc)
        tmp = f.name
    # bsk fill 的 --value 太长会顶到命令行上限，用参数文件形式塞进去
    p = bsk("fill", "--session", args.session, "--observation", st.get("observation_id") or obs,
            "--ref", ref, "--value", desc, "--timeout", "120s", check=False)
    ok = p.returncode == 0
    log("填充 " + ("成功" if ok else "失败: " + (p.stderr or "")[:200]))
    if not ok:
        return 1

    time.sleep(2)
    if args.dry:
        log("dry-run：没有提交")
        return 0

    ref2 = find_ref(sync(args.session).get("vom") or "", "立即投稿")
    if not ref2:
        raise RuntimeError("没找到提交按钮")
    st2 = sync(args.session)
    bsk("click", "--session", args.session, "--observation", st2.get("observation_id") or "",
        "--ref", ref2, check=False)
    time.sleep(10)
    vom = sync(args.session).get("vom") or ""
    log("提交结果：" + ("投递成功" if "投递成功" in vom else "没看到成功页，去看截图"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
