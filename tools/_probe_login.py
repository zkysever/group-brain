"""只验证「cookie 注入能不能登录投稿号」，不碰视频。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_bsk as P  # noqa: E402

cookie = (Path(os.environ["USERPROFILE"]) / ".dsh" / "secrets" / "bili-upload-cookie.txt").read_text(encoding="utf-8").strip()
P.ensure_session()
P.login_via_cookie(cookie)
st = P.sync()
print("url:", st.get("url"))
print("title:", (st.get("title") or "")[:60])
vom = st.get("vom") or ""
for kw in ("上传视频", "短信验证", "登录", "发布视频"):
    print(f"  {kw}: {kw in vom}")
