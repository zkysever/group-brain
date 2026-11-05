"""中文分词：把一句话展开成 bigram token 串，喂给 SQLite FTS5。

为什么不用 jieba：
  1. 少一个依赖，Windows 上装 jieba 没问题但没必要；
  2. bigram 在两字词、专名、口头禅上召回更好（"大肥鱼" "别催我" 这类）；
  3. 索引和查询走同一套逻辑，不会出现"建库时切了、查询时没切"的错位。

规则：
  - 连续汉字 -> 相邻两字切片；单字原样保留
  - 英文字母 / 数字 -> 按词，转小写
  - 其他符号 -> 当作分隔符丢弃
"""
from __future__ import annotations

import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ASCII_WORD = re.compile(r"[a-zA-Z0-9_]+")
_SPLIT = re.compile(r"[^\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff a-zA-Z0-9_]+")


def _cjk_bigrams(run: str) -> list[str]:
    if not run:
        return []
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def to_bigrams(text: str) -> str:
    """返回空格分隔的 token 串，可直接写进 fts5 表。"""
    if not text:
        return ""
    tokens: list[str] = []
    # 先按非中文/非字母数字切段，再逐段处理
    for chunk in _SPLIT.split(text):
        if not chunk:
            continue
        buf: list[str] = []
        for ch in chunk:
            if _CJK.match(ch):
                buf.append(ch)
            else:
                if buf:
                    tokens.extend(_cjk_bigrams("".join(buf)))
                    buf = []
        if buf:
            tokens.extend(_cjk_bigrams("".join(buf)))
        for w in _ASCII_WORD.findall(chunk):
            tokens.append(w.lower())
    # 去重会影响 bm25 打分，保留重复
    return " ".join(tokens)


def clean_text(text: str) -> str:
    """压掉多余空白，方便展示与入库。"""
    return re.sub(r"\s+", " ", text or "").strip()
