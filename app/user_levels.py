# -*- coding: utf-8 -*-
"""用户等级：谁算「关键用户」。

等级只有一条判定链，不允许两处都能改：

  1. `data/people.json` 有记录 -> 用记录里的 level（人工设定过，最高优先）
  2. 没有记录，但命中 `people.owners` / `group.key_users` -> 用种子等级
     种子是「首次部署的常量」，不是第二份状态：一旦面板给这个 wxid 建了记录，
     读记录就不再落到种子，改一处只有一处生效。
  3. 都没有 -> `people.default_level`（默认 stranger）

为什么不写进 `chats.json`：那是会话（群/人）维度，一个人出现在十个群里就会被
复制成十份，改一处另九处不知道 —— 项目已经因为同一种错吃过苦。
为什么不写进记忆库：等级是权限边界，不能被容量淘汰、不能被语义检索捞出来，
更不能被模型自己的 remember 改写。

`blocked` 是「不理」，不是「低等级」：它不进记录、不进上下文、不占名额。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

LEVELS = ("owner", "friend", "stranger", "blocked")
# 「关键用户」= 这两个等级。记录、接话、群内记忆都只认它们。
KEY_LEVELS = ("owner", "friend")

LEVEL_LABEL = {
    "owner": "主人",
    "friend": "熟人",
    "stranger": "陌生人",
    "blocked": "不理",
}
LEVEL_HINT = {
    "owner": "自己人：回复更主动，上下文更长，群里说的话会被记下来",
    "friend": "熟人：可以主动接话，群里说的话会被记下来",
    "stranger": "默认：只在被 @ 或被叫到名字时回，群里不留痕",
    "blocked": "完全不处理：不回、不记、不进上下文",
}


def is_key_level(level: str) -> bool:
    return str(level or "") in KEY_LEVELS


def _norm_set(items: Optional[Iterable[Any]]) -> List[str]:
    """把配置里的名单洗成小写字符串列表。昵称大小写不敏感。"""
    out: List[str] = []
    for raw in items or []:
        text = str(raw or "").strip().lower()
        if text and text not in out:
            out.append(text)
    return out


class UserLevelStore:
    """等级的读写门面。所有落盘都在这里，别处不许直接碰 people.json。"""

    def __init__(
        self,
        path: Path,
        owners: Optional[Sequence[Any]] = None,
        key_users: Optional[Sequence[Any]] = None,
        default: str = "stranger",
    ) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = {}
        # 种子：只在「这个人还没有记录」时生效，绝不回写 people.json
        self._owners = _norm_set(owners)
        self._keys = _norm_set(key_users)
        self._default = default if default in LEVELS else "stranger"
        self.load_error = ""
        self._load()

    # ------------------------------------------------------------ 磁盘
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            # 坏了就按空库启动：等级读不出来只是「多回了几句」，不该让机器人起不来
            self.load_error = str(exc)
            logger.warning("people.json 坏了，按默认等级启动：%s", exc)
            return
        items = raw.get("people") if isinstance(raw, dict) else None
        if not isinstance(items, dict):
            return
        for wxid, value in items.items():
            key = str(wxid or "").strip()
            if not key or not isinstance(value, dict):
                continue
            level = str(value.get("level") or "").strip()
            self._data[key] = {
                "level": level if level in LEVELS else self._default,
                "alias": str(value.get("alias") or ""),
                "note": str(value.get("note") or ""),
                "updated": float(value.get("updated") or 0),
            }

    def _save(self) -> None:
        """先写临时文件再整体替换：写一半断电会留下一个读不出来的 json。"""
        payload = {"updated": time.time(), "people": self._data}
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_suffix(".tmp")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)

    # ------------------------------------------------------------ 读
    def level_of(self, wxid: str, name: str = "") -> str:
        """这个人算哪一级。wxid 优先，昵称只用于兜住配置里的手填名单。"""
        key = str(wxid or "").strip()
        nick = str(name or "").strip().lower()
        with self._lock:
            record = self._data.get(key) if key else None
            if record is not None:
                return str(record.get("level") or self._default)
        if key and key.lower() in self._owners:
            return "owner"
        if nick and nick in self._owners:
            return "owner"
        if key and key.lower() in self._keys:
            return "friend"
        if nick and nick in self._keys:
            return "friend"
        return self._default

    def is_key(self, wxid: str, name: str = "") -> bool:
        return is_key_level(self.level_of(wxid, name))

    def alias_of(self, wxid: str) -> str:
        with self._lock:
            return str((self._data.get(str(wxid or "").strip()) or {}).get("alias") or "")

    def note_of(self, wxid: str) -> str:
        with self._lock:
            return str((self._data.get(str(wxid or "").strip()) or {}).get("note") or "")

    def get(self, wxid: str) -> Dict[str, Any]:
        key = str(wxid or "").strip()
        with self._lock:
            record = dict(self._data.get(key) or {})
        if record:
            record["wxid"] = key
            record["seeded"] = False
            return record
        level = self.level_of(key)
        return {"wxid": key, "level": level, "alias": "", "note": "",
                "updated": 0, "seeded": level != self._default}

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def count(self) -> int:
        with self._lock:
            return len(self._data)

    def apply_seeds(self, owners: Optional[Sequence[Any]] = None,
                    key_users: Optional[Sequence[Any]] = None,
                    default: Optional[str] = None) -> None:
        """配置热改后同步种子。不重建对象——面板改完下一条消息就该按新值判。

        注意：这里改的是「种子」，不会碰 people.json 里已有的显式记录。
        否则面板调一次默认等级，就把用户手工设过的等级全冲掉了。
        """
        with self._lock:
            if owners is not None:
                self._owners = _norm_set(owners)
            if key_users is not None:
                self._keys = _norm_set(key_users)
            if default is not None and str(default) in LEVELS:
                self._default = str(default)

    def describe(self) -> Dict[str, Any]:
        return {
            "default": self._default,
            "levels": [{"key": k, "label": LEVEL_LABEL[k], "hint": LEVEL_HINT[k]}
                       for k in LEVELS],
            "key_levels": list(KEY_LEVELS),
            "owners": list(self._owners),
            "key_users": list(self._keys),
            "count": self.count(),
        }

    # ------------------------------------------------------------ 写
    def set(self, wxid: str, level: Optional[str] = None,
            alias: Optional[str] = None, note: Optional[str] = None) -> Dict[str, Any]:
        """只改传进来的字段。返回改完之后的记录。

        Raises:
            ValueError: wxid 为空，或 level 不在 LEVELS 里。
        """
        key = str(wxid or "").strip()
        if not key:
            raise ValueError("没有 wxid")
        with self._lock:
            if level is not None and str(level) not in LEVELS:
                raise ValueError("等级只能是 " + " / ".join(LEVELS))
            item = self._data.setdefault(
                key, {"level": self._default, "alias": "", "note": "", "updated": 0}
            )
            if level is not None:
                item["level"] = str(level)
            if alias is not None:
                item["alias"] = str(alias or "").strip()
            if note is not None:
                item["note"] = str(note or "").strip()
            item["updated"] = time.time()
            self._save()
            return dict(item)

    def forget(self, wxid: str) -> bool:
        """删掉显式记录，这个人回落到种子/默认等级。"""
        key = str(wxid or "").strip()
        with self._lock:
            if key not in self._data:
                return False
            self._data.pop(key, None)
            self._save()
            return True

    def ensure_file(self) -> bool:
        """文件不存在就落一个空库，方便用户直接打开手改。

        只在 `Bot` 启动时调一次。面板每 2 秒轮询一次 `describe()`，
        那个路径上不能写盘 —— 这里做成「存在就什么都不做」。
        写不出来不算错：读不到是空库，照样按默认等级跑。

        Returns:
            bool: True 表示这次真的落了盘。
        """
        with self._lock:
            if self.path.exists():
                return False
            try:
                self._save()
                return True
            except OSError as exc:
                logger.warning("people.json 建不出来（等级仍可用，只是不落盘）：%s", exc)
                return False
