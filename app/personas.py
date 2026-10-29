# -*- coding: utf-8 -*-
"""多角色（人设）库。

一个人设就是一个 md 文件，放在 `prompts/personas/` 下，文件名就是角色名。
**这里只管两件事：有哪些角色、默认用哪个**（`data/personas.json` 的 `default`）。
「某个会话用哪个角色」存在 `data/chats.json`（ChatPrefs），不在这里——
同一件事存两处，改一边另一边必然走样，第一版踩过，已经删掉了那份副本。

为什么不用 config.yaml 存绑定：会话 id 是 wxid / xxx@chatroom 这种运行时才有的东西，
写进配置会让人误以为要手工维护。它们放 data/，坏了删掉重设就行。

兼容：以前只有一个 `prompts/persona.md`。第一次启动时如果 personas/ 是空的，
会自动把它复制成 `deepChat.md` 并设为默认——老用户无感升级。
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_NAME = "deepChat"
MAX_CHARS = 20000
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\r\n\t]')


class PersonaError(ValueError):
    """角色名不合法 / 角色不存在 / 删不得。"""


def safe_name(name: str) -> str:
    """把用户输入的角色名洗成文件安全的。中文和空格都留着，只挡路径分隔符之类。"""
    cleaned = _FORBIDDEN.sub("", (name or "").strip())
    cleaned = cleaned.strip(". ")
    if not cleaned:
        raise PersonaError("角色名不能为空，也不能只有符号")
    if len(cleaned) > 24:
        raise PersonaError("角色名太长了（最多 24 个字）")
    return cleaned


class PersonaStore:
    def __init__(self, dir_path: Path, legacy_file: Optional[Path] = None,
                 state_path: Optional[Path] = None) -> None:
        self.dir = Path(dir_path)
        self.legacy_file = Path(legacy_file) if legacy_file else None
        self.state_path = Path(state_path) if state_path else self.dir.parent.parent / "data" / "personas.json"
        self.dir.mkdir(parents=True, exist_ok=True)
        # 只存「默认是哪个角色」。按会话绑定角色是 chats.json 的事，这里不掺和。
        self._state: Dict[str, Any] = {"default": ""}
        self._load_state()
        self._migrate_legacy()

    # ------------------------------------------------------------ 状态
    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("personas.json 坏了，按空配置启动：%s", exc)
            return
        if isinstance(raw, dict):
            self._state["default"] = str(raw.get("default") or "")

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("personas.json 写不进去：%s", exc)

    def _migrate_legacy(self) -> None:
        """老版本只有一个 persona.md：搬成 personas/deepChat.md，只搬一次。"""
        if self.names():
            return
        text = ""
        if self.legacy_file and self.legacy_file.exists():
            try:
                text = self.legacy_file.read_text(encoding="utf-8")
            except OSError:
                text = ""
        if not text.strip():
            text = "你是一个在微信上聊天的人。说话短、口语、别端着。"
        self._write(DEFAULT_NAME, text)
        self._state["default"] = DEFAULT_NAME
        self._save_state()
        logger.info("人设库初始化：把旧 persona.md 迁成「%s」", DEFAULT_NAME)

    # ------------------------------------------------------------ 文件
    def _path(self, name: str) -> Path:
        return self.dir / f"{safe_name(name)}.md"

    def _write(self, name: str, text: str) -> None:
        path = self._path(name)
        path.write_text((text or "").strip() + "\n", encoding="utf-8")

    def names(self) -> List[str]:
        return sorted(p.stem for p in self.dir.glob("*.md") if p.is_file())

    def exists(self, name: str) -> bool:
        try:
            return self._path(name).exists()
        except PersonaError:
            return False

    def text(self, name: str) -> str:
        path = self._path(name)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    # ------------------------------------------------------------ 增删改
    def create(self, name: str, text: str = "") -> str:
        clean = safe_name(name)
        if self._path(clean).exists():
            raise PersonaError(f"已经有一个叫「{clean}」的角色了")
        if not text.strip():
            # 新角色默认给个骨架，比空白页好下手
            text = (
                f"你是「{clean}」，在微信上跟人聊天。\n\n"
                "## 说话方式\n- 短，一两句说完\n- 口语，不用列表和 Markdown\n\n"
                "## 边界\n- 不主动提自己是 AI\n"
            )
        self._write(clean, text)
        logger.info("新建角色：%s", clean)
        return clean

    def save(self, name: str, text: str) -> None:
        clean = safe_name(name)
        if not text.strip():
            raise PersonaError("角色内容不能为空")
        if len(text) > MAX_CHARS:
            raise PersonaError(f"太长了（上限 {MAX_CHARS} 字）")
        self._write(clean, text)
        logger.info("保存角色：%s（%d 字）", clean, len(text))

    def rename(self, old: str, new: str) -> str:
        """改名：动文件名和「默认角色」。

        会话绑定改不到——它在 `data/chats.json` 里，不归这个类管。
        跟着改绑定的是面板接口（`api/personas/rename`），改完返回 `moved` 条数。
        """
        src = safe_name(old)
        dst = safe_name(new)
        if not self.exists(src):
            raise PersonaError("没有这个角色")
        if src == dst:
            return dst
        if self.exists(dst):
            raise PersonaError(f"已经有一个叫「{dst}」的角色了")
        try:
            self._path(src).rename(self._path(dst))
        except OSError as exc:
            raise PersonaError(f"改名失败：{exc}") from exc
        if self._state.get("default") == src:
            self._state["default"] = dst
        self._save_state()
        logger.info("角色改名：%s → %s", src, dst)
        return dst

    def delete(self, name: str) -> None:
        clean = safe_name(name)
        names = self.names()
        if clean not in names:
            raise PersonaError("没有这个角色")
        if len(names) <= 1:
            raise PersonaError("最后一个角色删不得，不然没东西可用")
        path = self._path(clean)
        try:
            path.unlink()
        except OSError as exc:
            raise PersonaError(f"删不掉：{exc}") from exc
        # 删了默认角色 → 默认换成剩下第一个
        if self.default_name == clean:
            self._state["default"] = self.names()[0]
        self._save_state()
        logger.info("删除角色：%s", clean)

    # ------------------------------------------------------------ 默认与绑定
    @property
    def default_name(self) -> str:
        name = self._state.get("default") or ""
        if name and self.exists(name):
            return name
        names = self.names()
        return names[0] if names else DEFAULT_NAME

    def set_default(self, name: str) -> str:
        clean = safe_name(name)
        if not self.exists(clean):
            raise PersonaError("没有这个角色")
        self._state["default"] = clean
        self._save_state()
        logger.info("默认角色改为：%s", clean)
        return clean

    def text_for(self, chat_id: str = "") -> str:
        """默认角色的人设。按会话选角色是 ChatPrefs 的事，这里只管「默认用谁」。"""
        text = self.text(self.default_name)
        if text:
            return text
        return "你是一个在微信上聊天的人。说话短、口语、别端着。"

    # ------------------------------------------------------------ 给面板看
    def describe(self) -> Dict[str, Any]:
        default = self.default_name
        items = []
        for name in self.names():
            path = self._path(name)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            try:
                chars = len(path.read_text(encoding="utf-8").strip())
            except OSError:
                chars = 0
            items.append(
                {
                    "name": name,
                    "chars": chars,
                    "updated": time.strftime("%m-%d %H:%M", time.localtime(mtime)),
                    "is_default": name == default,
                    "bound": 0,   # 由 server 按 chats.json 现算
                }
            )
        return {"default": default, "items": items}
