# -*- coding: utf-8 -*-
"""回复切分：一条长回复切成几条短消息发出去。

微信里一口气甩 200 字像公告，分两三条短句才像人在打字。
切分只在句末标点和换行处做，不会把一句话劈成两半。
"""
from __future__ import annotations

import re
from typing import List

SENTENCE_END = "。！？!?…~～\n"


def split_reply(text: str, max_chars: int = 40, max_bubbles: int = 3) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # 先按换行/句末切成小段
    pieces: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in SENTENCE_END and len(buf.strip()) >= 4:
            pieces.append(buf.strip())
            buf = ""
    if buf.strip():
        pieces.append(buf.strip())

    # 再合成长度合适的几条
    bubbles: List[str] = []
    current = ""
    for piece in pieces:
        candidate = (current + piece).strip() if current else piece
        if current and len(candidate) > max_chars:
            bubbles.append(current.strip())
            current = piece
        else:
            current = candidate
    if current.strip():
        bubbles.append(current.strip())

    # 条数超了就把它并回最后一条，宁可长一点也不刷屏
    if len(bubbles) > max_bubbles:
        bubbles = [*bubbles[: max_bubbles - 1], "".join(bubbles[max_bubbles - 1:])]

    return [b for b in bubbles if b][:max_bubbles]


def clean_for_wechat(text: str) -> str:
    """去掉微信里显示很丑的 Markdown 痕迹。"""
    text = re.sub(r"^\s*[-*]\s+", "· ", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 \2", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
