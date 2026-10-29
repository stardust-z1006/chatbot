# -*- coding: utf-8 -*-
"""按会话的监听策略。

微信里的人和群很多，但 deepChat 不该对谁都一样：有的人要好好聊，有的群只在被 @ 时
搭一句，有的群你想「看着但别插嘴」，还有的干脆别理。

四种模式：

| 模式 | 行为 | 典型场景 |
|---|---|---|
| `auto` | 正常回 | 私聊、朋友的群 |
| `at`   | 只在被 @ 或命中关键词时回 | 大群、工作群 |
| `silent` | 收下来但不回、也不记 | 只想看，不想插嘴的群 |
| `off`  | 完全不看（连日志都不写） | 不想让机器人碰到的地方 |

存在 `data/chats.json`，改了**下一条消息就生效**，不用重启。
没设过的会话用 config 里的默认值（`group_at_only` / `reply_private`）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MODES = ("auto", "at", "silent", "off")
MODE_LABEL = {
    "auto": "正常回复",
    "at": "只在 @ 时回",
    "silent": "只看不回",
    "off": "完全不看",
}
MODE_HINT = {
    "auto": "进来什么就回什么",
    "at": "要 @ 或者带关键词才回，其余当没看见",
    "silent": "收下来但不回、不记，日志里留一句",
    "off": "连日志都不记，彻底不管",
}
PEOPLE_LIMIT = 500
# 群里的成员规则：这群里谁说了算
PEOPLE_MODES = ("", "only", "ignore")
PEOPLE_LABEL = {"": "所有人都算", "only": "只理这些人", "ignore": "不理这些人"}


def is_group(chat_id: str) -> bool:
    return str(chat_id or "").endswith("@chatroom")


class ChatPrefs:
    """会话策略的读写。文件很小，整份读进内存，改完立刻落盘。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------ 磁盘
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("chats.json 坏了，按默认策略启动：%s", exc)
            return
        items = raw.get("chats") if isinstance(raw, dict) else None
        if isinstance(items, dict):
            for chat_id, value in items.items():
                if isinstance(value, dict):
                    people = value.get("people")
                    self._data[str(chat_id)] = {
                        "mode": str(value.get("mode") or "") or "",
                        "persona": str(value.get("persona") or "") or "",
                        "name": str(value.get("name") or "") or "",
                        "people_mode": str(value.get("people_mode") or "") or "",
                        "people": [str(x) for x in people if str(x).strip()]
                        if isinstance(people, list) else [],
                        "updated": float(value.get("updated") or 0),
                    }

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"updated": time.time(), "chats": self._data}
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("chats.json 写不进去：%s", exc)

    # ------------------------------------------------------------ 读
    def get(self, chat_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data.get(str(chat_id), {}))

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def mode_for(self, chat_id: str, cfg: Any) -> str:
        """这个会话该用什么模式。没单独设过就按 config 的默认值推。"""
        explicit = self.get(chat_id).get("mode")
        if explicit in MODES:
            return explicit
        if is_group(chat_id):
            return "at" if bool(cfg.get("wechat.group_at_only", True)) else "auto"
        return "auto" if bool(cfg.get("wechat.reply_private", True)) else "off"

    def persona_for(self, chat_id: str) -> str:
        return str(self.get(chat_id).get("persona") or "")

    def people_rule(self, chat_id: str) -> Dict[str, Any]:
        item = self.get(chat_id)
        return {
            "people_mode": str(item.get("people_mode") or ""),
            "people": list(item.get("people") or []),
        }

    def allow_sender(self, chat_id: str, sender: str = "", sender_name: str = "") -> bool:
        """群里这个人说的话算不算数。

        名单里存的是「wxid 或群昵称」，两种都认——面板上用户看到的是昵称，
        但昵称会改、也会重复，所以 wxid 也留着匹配。
        没设规则、或者规则是 only 但名单是空的，都按「所有人都算」处理：
        空名单要是按 only 理解，这群就彻底没人能说话了，属于配置事故。
        """
        item = self.get(chat_id)
        mode = str(item.get("people_mode") or "")
        people = [str(x) for x in (item.get("people") or [])]
        if mode not in ("only", "ignore") or not people:
            return True
        sender = str(sender or "").strip()
        sender_name = str(sender_name or "").strip()
        keys = {sender, sender_name, sender_name.lower()}
        hit = any((name in keys) or (name.lower() in keys) for name in people if name)
        return hit if mode == "only" else not hit

    # ------------------------------------------------------------ 写
    def set(self, chat_id: str, mode: Optional[str] = None,
            persona: Optional[str] = None, name: str = "",
            people_mode: Optional[str] = None,
            people: Optional[List[str]] = None) -> Dict[str, Any]:
        """只改传进来的字段；mode/persona/people_mode 传空串表示恢复默认。"""
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            raise ValueError("没有会话 id")
        with self._lock:
            item = self._data.setdefault(
                chat_id,
                {"mode": "", "persona": "", "name": "", "people_mode": "", "people": [], "updated": 0},
            )
            if mode is not None:
                if mode and mode not in MODES:
                    raise ValueError("模式只能是 " + " / ".join(MODES))
                item["mode"] = mode
            if persona is not None:
                item["persona"] = str(persona or "")
            if name:
                item["name"] = name
            if people_mode is not None:
                if people_mode and people_mode not in PEOPLE_MODES:
                    raise ValueError("成员规则只能是 only / ignore / 空")
                item["people_mode"] = people_mode
            if people is not None:
                cleaned = []
                for raw in people:
                    text = str(raw or "").strip()
                    if text and text not in cleaned:
                        cleaned.append(text)
                if len(cleaned) > 200:
                    raise ValueError("名单最多 200 个人")
                item["people"] = cleaned
            item["updated"] = time.time()
            # 全空就是「和默认一样」，别在文件里堆垃圾
            if (not item["mode"] and not item["persona"] and not item["name"]
                    and not item["people_mode"] and not item["people"]):
                self._data.pop(chat_id, None)
            self._save()
            return dict(self._data.get(chat_id, {}))

    def forget(self, chat_id: str) -> None:
        with self._lock:
            self._data.pop(str(chat_id), None)
            self._save()

    def count(self) -> int:
        with self._lock:
            return len(self._data)
