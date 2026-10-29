# -*- coding: utf-8 -*-
"""原始消息内容 → 能进模型的文本。

微信库里存的 `content` 只有两种形状：

  - 文本：就是人写的字；
  - `local_type=49`（文件 / 链接 / 卡片 / 引用 / 合并转发）：一整段 appmsg XML。

以前那段 XML 被原样当成「链接」的正文喂进上下文，后果有两个：模型会照着
XML 编造「你发的链接讲的是××」，而入库判据又会把这段 XML 当成一条陈述记下来。
这里把它折叠成一句人话。

另一半职责是占位：图片 / 语音 / 视频 / 表情没有文字可读。上层以前直接丢弃
（对方发图问「这怎么办」，机器人一声不吭）。现在给一个明确标记，让模型知道
自己漏了什么、能顺着问一句，而不是假装没收到。

为什么占位符要把「看不到」写在里面：机器人的 persona 是用户自己写的，不能指望
每份 persona 都记得提醒模型「你看不见图」。标记自带这层含义，才不会出现
「这张图拍得真好看」这种凭空编造。
"""
from __future__ import annotations

import html
import re
from typing import Tuple

# 占位标记。字符串本身就是给模型看的说明，改这里等于改行为。
PLACEHOLDER = {
    "image": "[图片·看不到内容]",
    "voice": "[语音·听不到声音]",
    "video": "[视频·看不到内容]",
    "emoji": "[表情]",
    "location": "[位置]",
    "card": "[卡片]",
    "link": "[卡片]",
    "redpacket": "[红包]",
}

# 正文天生是机器编码的类型：折也折不出人话，一律给标记。
_ALWAYS_MARK = frozenset({"image", "video", "emoji", "location", "redpacket"})

# 这些类型不是「对方说了什么」，进模型只会造成误读。
#   system：入群 / 撤回 / 拍一拍等系统提示，它们是事件不是对话
#   call：音视频通话记录（「已拒绝」「通话时长 03:21」）
IGNORED_TYPES = frozenset({"system", "call"})

# 折叠后的正文上限。一条 appmsg 的 <des> 可能几百字，全塞进去白烧 token。
MAX_TEXT = 120

# appmsg 的 <type>。没列到的一律按「卡片」处理，不猜。
_APPMSG_LABEL = {
    3: "音乐",
    4: "视频",
    5: "链接",
    6: "文件",
    8: "表情",
    19: "合并转发",
    33: "小程序",
    36: "小程序",
    51: "视频号",
    57: "引用",
    63: "直播",
    2000: "转账",
    2001: "红包",
}

_PLACEHOLDERS = frozenset(PLACEHOLDER.values())

# 「只有标签、没有正文」的折叠结果。这些同样算占位（没有真实内容可读）。
_BARE_LABELS = frozenset(
    {"卡片", "链接", "文件", "音乐", "视频", "表情", "小程序", "视频号",
     "引用", "直播", "合并转发", "转账", "红包"}
)

# 一段连续的 base64 / 十六进制长串。appmsg 里混着缩略图、签名、密钥，
# 留在正文里既占 token 又会被模型当成「神秘代码」。
_BLOB = re.compile(r"[A-Za-z0-9+/=]{40,}")
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_WS = re.compile(r"[ \t\u3000]+")
_HOST = re.compile(r"https?://([^/?#]+)")


def _tag(name: str) -> re.Pattern:
    """取一个标签的文本内容。容忍 CDATA、属性、缺失。"""
    return re.compile(rf"<{name}(?:\s[^>]*)?>(.*?)</{name}>", re.S)


_TITLE = _tag("title")
_DES = _tag("des")
_TYPE = _tag("type")
_FILENAME = _tag("filename")
_FILEEXT = _tag("fileext")
_TOTAL = _tag("totallen")
_URL = _tag("url")
_REF_CONTENT = _tag("content")
_REF_NAME = _tag("displayname")


def looks_like_xml(text: str) -> bool:
    """只认「以 < 开头」这一条。

    不用 XML 解析器：微信的 appmsg 偶发非法字符（未转义的 & 、截断），
    解析器一抛异常整条消息就没了。这里是「拿到多少算多少」的场景，
    正则比真解析更耐操。
    """
    return text.lstrip().startswith("<")


def _clean(text: str) -> str:
    if not text:
        return ""
    t = _CDATA.sub(r"\1", text)
    t = _BLOB.sub("", t)
    t = html.unescape(t)
    t = re.sub(r"<[^>]+>", " ", t)      # 残留标签（嵌套 XML）
    t = _WS.sub(" ", t)
    return t.strip()


def _one(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(text)
    return _clean(m.group(1)) if m else ""


def fold_appmsg(raw: str) -> str:
    """把 type=49 的 appmsg XML 折成一句人话。折不动就返回空串。"""
    if not raw or not looks_like_xml(raw):
        return ""

    kind_raw = _one(_TYPE, raw)
    try:
        label = _APPMSG_LABEL.get(int(kind_raw), "卡片")
    except (TypeError, ValueError):
        label = "卡片"

    title = _one(_TITLE, raw)
    des = _one(_DES, raw)
    body = title or des

    if label == "文件":
        name = _one(_FILENAME, raw) or title
        ext = _one(_FILEEXT, raw)
        if not name and ext:
            name = f".{ext}"
        size = _one(_TOTAL, raw)
        if size.isdigit() and int(size) > 0:
            body = f"{name}（{_size(int(size))}）" if name else _size(int(size))
        else:
            body = name
    elif label == "引用":
        # 引用的正文在外面那层 title（这个人说的话），被引用的在 refermsg 里
        quoted = _one(_REF_CONTENT, raw)
        who = _one(_REF_NAME, raw)
        head = f"{who}：{quoted}" if who and quoted else (quoted or who)
        body = f"{head} → {title}" if head and title else (head or title)
    elif not body and label == "链接":
        host = _HOST.search(raw)
        body = host.group(1) if host else ""

    body = _clip(body)
    return f"[{label}] {body}".strip() if body else f"[{label}]"


def _size(num: int) -> str:
    if num >= 1024 * 1024:
        return f"{num / 1048576:.1f}MB"
    if num >= 1024:
        return f"{num / 1024:.0f}KB"
    return f"{num}B"


def _clip(text: str) -> str:
    t = (text or "").strip()
    return t if len(t) <= MAX_TEXT else t[:MAX_TEXT].rstrip() + "…"


def render(kind: str, content: str) -> Tuple[str, bool]:
    """把一条消息渲染成「能进模型的文本」。

    Args:
        kind: 已归一化的类型（text / image / voice / link / …）。
        content: 库里的原始正文。

    Returns:
        ``(文本, 是否是占位)``。文本为空表示这条消息不该进上下文；
        占位为 True 表示模型只看到标记、看不到真实内容，调用方据此决定
        要不要写记忆。
    """
    raw = (content or "").strip()
    if kind in IGNORED_TYPES:
        return "", False
    if kind == "text":
        return raw, False

    # 正文是文件名 / md5 / 坐标 / 封面 id，折不出人话，直接给标记。
    if kind in _ALWAYS_MARK:
        return PLACEHOLDER[kind], True

    # 语音：微信端开了「聊天中的语音消息自动转文字」时，正文就是转写文本，
    # 那是真实内容，直接当文本用（比占位有用得多）。
    if kind == "voice":
        if raw and not looks_like_xml(raw):
            return raw, False
        return PLACEHOLDER["voice"], True

    if raw and looks_like_xml(raw):
        folded = fold_appmsg(raw)
        # 只有标签、没有正文（截断的 XML / 没见过的卡片类型）＝ 看不到真实内容，
        # 按占位处理，别让它混进记忆库。
        return folded, folded.strip().startswith("[") and folded.strip().endswith("]")

    if raw:
        # 不认识的类型但有正文：当文本用。宁可多说一句，也不要把内容吞掉。
        return raw, False
    mark = PLACEHOLDER.get(kind) or f"[{kind}]"
    return mark, True


def is_placeholder(text: str) -> bool:
    """这段文本是不是「只有占位、没有真内容」。

    合并窗口会把连着来的几条拼成一句，所以逐行判断、全中才算——
    混着真话的消息仍然值得记。

    两类都算：带说明的占位（`[图片·看不到内容]`）和只有标签的折叠结果
    （`[卡片]`）。后者是 XML 折不出正文时的兜底，同样没有信息量。
    只标签的情形按白名单认，避免把用户真发的「[好]」也当占位。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False
    return all(_is_mark(line) for line in lines)


def _is_mark(line: str) -> bool:
    if line in _PLACEHOLDERS:
        return True
    if len(line) > 12 or not (line.startswith("[") and line.endswith("]")):
        return False
    return line[1:-1] in _BARE_LABELS
