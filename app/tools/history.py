# -*- coding: utf-8 -*-
"""翻微信聊天记录。

只读本地数据库（wechatauto 的 WeChatDB），不驱动界面、不会触发任何发送。
范围有上限：默认只往回翻 days 天 / 最多 600 条，避免一次把几万条塞进上下文。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional

from .registry import Tool

logger = logging.getLogger(__name__)

PAGE = 200
MAX_SCAN = 600


def _readable(text: Any) -> str:
    """把 XML 卡片/引用消息压成一句，别把几百字标签塞进上下文。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.startswith(("<?xml", "<msg")) or "<appmsg" in raw:
        match = re.search(r"<title>(.*?)</title>", raw, re.S)
        title = (match.group(1).strip() if match else "")
        return "[卡片] " + title if title else "[卡片消息]"
    if raw.startswith("<") and raw.endswith(">"):
        return "[富文本消息]"
    return raw


def _fmt_time(ts: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "??"


class HistoryReader:
    def __init__(self, db: Any, self_wxid: str = ""):
        self.db = db
        self.self_wxid = self_wxid or ""
        self._nicks: Dict[str, str] = {}

    # -- 名字 ---------------------------------------------------------
    def _nick(self, wxid: str) -> str:
        if not wxid:
            return ""
        if wxid == self.self_wxid:
            return "我"
        if wxid in self._nicks:
            return self._nicks[wxid]
        try:
            name = self.db.get_nickname(wxid) or wxid
        except Exception:
            name = wxid
        self._nicks[wxid] = name
        return name

    def resolve(self, name: str) -> Optional[str]:
        name = (name or "").strip()
        if not name:
            return None
        if name.endswith("@chatroom") or name.startswith(("wxid_", "gh_")):
            return name
        if name in ("文件传输助手", "filehelper"):
            return "filehelper"
        try:
            for hit in self.db.search_contact(name):
                if name in (hit.get("nick_name"), hit.get("remark")):
                    return hit["username"]
        except Exception:
            pass
        try:
            for user, nick in (self.db.nickname_map() or {}).items():
                if nick == name:
                    return user
        except Exception:
            pass
        return None

    # -- 读消息 -------------------------------------------------------
    def scan(self, username: str, days: float = 7, cap: int = MAX_SCAN) -> List[Dict[str, Any]]:
        cutoff = time.time() - max(0.1, days) * 86400
        out: List[Dict[str, Any]] = []
        offset = 0
        while len(out) < cap:
            try:
                rows = self.db.get_messages(username, limit=PAGE, offset=offset)
            except Exception as exc:
                logger.warning("读聊天记录失败（%s）：%s", username, exc)
                break
            if not rows:
                break
            for row in rows:
                if int(row.get("create_time") or 0) < cutoff:
                    return out
                out.append(row)
                if len(out) >= cap:
                    return out
            offset += len(rows)
            if len(rows) < PAGE:
                break
        return out

    def format(self, rows: List[Dict[str, Any]]) -> str:
        lines = []
        for row in reversed(rows):  # 时间正序，读起来自然
            who = self._nick(row.get("sender_username") or "") or (
                "我" if row.get("sender_id") == 2 else "对方"
            )
            text = _readable(row.get("content")).replace("\n", " ").strip()
            if not text:
                continue
            lines.append("%s %s：%s" % (_fmt_time(row.get("create_time")), who, text[:300]))
        return "\n".join(lines)


def make_tool(reader: HistoryReader, cfg, current_chat: str = "") -> Tool:
    default_days = float(cfg.get("history.days", 7))

    def chat_history(query: str = "", chat: str = "",
                     days: float = 0, scope: str = "当前") -> str:
        span = days if days > 0 else default_days
        target = (chat or "").strip()

        if scope == "全部" or target == "全部":
            return _search_all(reader, query, span)

        username = reader.resolve(target) if target else current_chat
        if not username:
            return "（不知道该翻哪个会话，让对方说清楚是谁）"
        rows = reader.scan(username, days=span)
        if not rows:
            return "（这段时间里没找到聊天记录）"

        if query:
            kw = query.strip().lower()
            rows = [r for r in rows if kw in str(r.get("content") or "").lower()]
            if not rows:
                return "（翻过了，没有提到「%s」的消息）" % query
        return reader.format(rows[:120]) or "（都是空消息）"

    def _search_all(reader: HistoryReader, query: str, days: float) -> str:
        if not query:
            return "（全库搜索必须给关键词）"
        try:
            sessions = reader.db.get_sessions(limit=20)
        except Exception as exc:
            return f"（取会话列表失败：{exc}）"

        kw = query.strip().lower()
        hits: List[str] = []
        for session in sessions:
            username = session.get("username")
            if not username:
                continue
            rows = reader.scan(username, days=days, cap=200)
            matched = [r for r in rows if kw in str(r.get("content") or "").lower()]
            if not matched:
                continue
            name = reader._nick(username) or username
            hits.append("【%s】\n%s" % (name, reader.format(matched[:20])))
        return "\n\n".join(hits)[:8000] or "（都没有提到「%s」）" % query

    return Tool(
        name="chat_history",
        description=(
            "翻微信聊天记录。对方说「上次那个」「你之前说的」「我们聊过」时用，"
            "或者要查某人在聊天里提过的具体信息。只读，不会给任何人发消息。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要找的关键词，可留空表示看最近聊天"},
                "chat": {"type": "string", "description": "会话名（昵称/备注）。留空=当前会话"},
                "days": {"type": "number", "description": "往回翻几天，默认按配置"},
                "scope": {
                    "type": "string",
                    "enum": ["当前", "全部"],
                    "description": "当前=只查指定会话，全部=在最近 20 个会话里搜",
                },
            },
            "required": [],
        },
        func=chat_history,
    )
