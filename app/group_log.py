# -*- coding: utf-8 -*-
"""关键用户在群里说过的话：只记录，不改判定。

**这是流水，不是记忆。** 两者的区别要写清楚，否则迟早长成第二套记忆库：

| | 记忆库（MemoryStore） | 这里（GroupLog） |
|---|---|---|
| 存什么 | 「关于这个人的事实」，已抽掉原话 | 原话 + 当时判了什么 |
| 会不会变 | 会：查重、合并、作废、淘汰 | 不会：只追加，永不改写 |
| 参与检索吗 | 会，进上下文 | 不会，只给人看、给脚本算 |
| 谁写 | 自动入库 + remember 工具 | 只由 on_message 写 |

将来群内记忆启用时，记忆条目上记一个 `msg_key` 指回这里，**而不是把原话抄一份** ——
同一条信息存两处，改一边另一边必然走样。

三个用途：
  1. 回溯：这条为什么回 / 为什么没回，事后能查（`replied` / `score` / `reasons`）
  2. 调参：`score` + `reasons` + `replied` 就是一份带标注的数据集，
     能离线算准召，比拍脑袋调阈值靠谱
  3. 导出：面板/脚本直接读 `data/group_log/*.jsonl`

存储选择：**一天一个文件**（`data/group_log/2026-10-26.jsonl`），UTF-8，
一行一个 JSON。理由：
  - 一行坏掉不影响其他行（memory.log 的经验）
  - 文件名即日期，排序即时间序，读最近 N 天不用扫描全库
  - 单日文件天然有界，不需要另写轮转逻辑

两种行，用 `op` 区分：
  - `{"op": "msg", ...}`    关键用户说的一句话
  - `{"op": "reply", ...}`  机器人对这批消息的回复（发送结果一并留下）

为什么不把回复回写到 msg 行里：追加日志的价值就是「永不就地修改」。
就地改一旦写一半断电，那条记录就既不是旧值也不是新值。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# 单条正文上限。群里的长文（复制粘贴的公告、转发）会把记录撑成几 KB，
# 而这里要的是「说了什么」，不是全文归档。
TEXT_CAP = 500
# 回复正文上限（同上，只留够追溯的长度）
REPLY_CAP = 200
# 去重窗口：同一条消息被重复投递（重启、库游标回退）只记一次
DUP_TTL = 86400
DUP_CAP = 4096
# 每写这么多行 fsync 一次。逐行 fsync 是上次踩过的坑（一次检索写 6 份 11KB），
# 这里是流水而不是「已承诺的事实」，掉电丢最后几行可以接受。
FSYNC_EVERY = 20
# 读的时候一次最多回多少条
READ_CAP = 500

MSG_FIELDS = (
    "op", "ts", "msg_time", "chat_id", "chat_name", "sender", "sender_name",
    "level", "alias", "msg_key", "msg_type", "text",
    "score", "reasons", "replied", "reply",
)


def _clip(text: str, cap: int) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + "…"


class GroupLog:
    """关键用户群消息的追加日志。所有落盘都在这里。"""

    def __init__(self, data_dir: Path, days: int = 30, enabled: bool = True) -> None:
        self.dir = Path(data_dir)
        self.days = max(1, int(days))
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self._dup: "OrderedDict[str, float]" = OrderedDict()
        self._pending = 0
        # stats() 的「行数缓存」：(文件字节数, 行数)。文件只追加，大小没变就不重数。
        self._count_cache: tuple = (-1, 0)
        self.write_error = ""
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 写
    def append_msg(
        self,
        *,
        chat_id: str,
        chat_name: str,
        sender: str,
        sender_name: str,
        level: str,
        alias: str,
        text: str,
        msg_key: str,
        msg_type: str = "text",
        msg_time: float = 0,
        score: Optional[int] = None,
        reasons: Optional[Iterable[str]] = None,
        now: Optional[float] = None,
    ) -> bool:
        """记一条关键用户的群消息。返回 False 表示没记（去重/为空/关掉了）。

        去重按 `msg_key`：同一个指纹一天内只记一次。这是最后一道防线，
        `_Seen` 管的是「要不要回」，这里管的是「要不要留痕」，两者不能互相顶替：
        被判定为不回的消息也要留痕，所以不能借用 `_Seen`。
        """
        if not self.enabled:
            return False
        text = _clip(text, TEXT_CAP)
        key = str(msg_key or "").strip()
        if not text or not key:
            return False
        stamp = float(now if now is not None else time.time())
        if self._is_dup(key, stamp):
            return False
        record = {
            "op": "msg",
            "ts": round(stamp, 3),
            "msg_time": int(msg_time or 0),
            "chat_id": str(chat_id or ""),
            "chat_name": str(chat_name or ""),
            "sender": str(sender or ""),
            "sender_name": str(sender_name or ""),
            "level": str(level or ""),
            "alias": str(alias or ""),
            "msg_key": key,
            "msg_type": str(msg_type or "text"),
            "text": text,
            # 这两个由接话判定填；判定还没接线时留 null，不要填 0 冒充「算了没分」
            "score": score,
            "reasons": list(reasons or []) if reasons is not None else None,
            "replied": None,
            "reply": "",
        }
        return self._write(record, stamp)

    def append_reply(
        self,
        *,
        chat_id: str,
        sender: str,
        text: str,
        ok: bool,
        msg_keys: Optional[Iterable[str]] = None,
        level: str = "",
        score: Optional[int] = None,
        reasons: Optional[Iterable[str]] = None,
        now: Optional[float] = None,
    ) -> bool:
        """记一次回复。单独成行，不回写 msg 行（见模块开头）。"""
        if not self.enabled:
            return False
        stamp = float(now if now is not None else time.time())
        record = {
            "op": "reply",
            "ts": round(stamp, 3),
            "chat_id": str(chat_id or ""),
            "sender": str(sender or ""),
            "level": str(level or ""),
            "msg_keys": [str(k) for k in (msg_keys or [])],
            "text": _clip(text, REPLY_CAP),
            "ok": bool(ok),
            "score": score,
            "reasons": list(reasons or []) if reasons is not None else None,
        }
        return self._write(record, stamp)

    def _is_dup(self, key: str, stamp: float) -> bool:
        with self._lock:
            old = self._dup.get(key)
            self._dup[key] = stamp
            self._dup.move_to_end(key)
            while len(self._dup) > DUP_CAP:
                self._dup.popitem(last=False)
        return old is not None and stamp - old < DUP_TTL

    def _write(self, record: Dict[str, Any], stamp: float) -> bool:
        line = json.dumps(record, ensure_ascii=False)
        path = self.path_for(stamp)
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(line + "\n")
                    fp.flush()
                    self._pending += 1
                    if self._pending >= FSYNC_EVERY:
                        os.fsync(fp.fileno())
                        self._pending = 0
            self.write_error = ""
            return True
        except OSError as exc:
            # 记录写不进去不该影响回复：留一句 warning 就够了。
            # 但要能被查到 —— 静默 pass 是上次评审里点名的问题。
            self.write_error = str(exc)
            logger.warning("群消息记录写不进去（%s）：%s", path, exc)
            return False

    def path_for(self, stamp: float) -> Path:
        return self.dir / (time.strftime("%Y-%m-%d", time.localtime(stamp)) + ".jsonl")

    # ------------------------------------------------------------ 读
    def read(
        self,
        chat_id: str = "",
        sender: str = "",
        limit: int = 100,
        days: Optional[int] = None,
    ) -> List[dict]:
        """读最近的记录，时间正序返回。chat/sender 留空表示不过滤。"""
        if not self.enabled:
            return []
        want = max(1, min(READ_CAP, int(limit or 100)))
        window = self.days if days is None else max(1, int(days))
        chat_id = str(chat_id or "").strip()
        sender = str(sender or "").strip()
        out: List[dict] = []
        for path in self._files_newest_first(window):
            for record in self._tail(path, want * 4):
                if chat_id and record.get("chat_id") != chat_id:
                    continue
                if sender and record.get("sender") != sender:
                    continue
                out.append(record)
                if len(out) >= want:
                    break
            if len(out) >= want:
                break
        out.reverse()
        return out[:want]

    def _files_newest_first(self, days: int) -> List[Path]:
        if not self.dir.exists():
            return []
        files = sorted(self.dir.glob("*.jsonl"), reverse=True)
        return files[:max(1, days)]

    def _tail(self, path: Path, want: int) -> List[dict]:
        """从文件尾部倒着读，够 want 条就停。

        为什么不整份读：一个活跃群一天几千行，面板每 2 秒轮询一次全量读会明显吃 CPU。
        """
        try:
            size = path.stat().st_size
            block = max(8192, min(size, want * 400))
            with path.open("rb") as fp:
                fp.seek(max(0, size - block))
                data = fp.read()
        except OSError as exc:
            logger.warning("读群消息记录失败（%s）：%s", path, exc)
            return []
        lines = data.split(b"\n")
        if size > block:
            lines = lines[1:]   # 第一行可能是半截
        records: List[dict] = []
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue        # 坏一行跳过，不影响其他行
            if isinstance(item, dict):
                records.append(item)
            if len(records) >= want:
                break
        # 这里是「从新往旧」，read() 最后统一翻回正序（跨文件也才对得上）
        return records

    # ------------------------------------------------------------ 维护
    def prune(self, now: Optional[float] = None) -> int:
        """删掉超过保留期的日文件。返回删了几个。"""
        if not self.enabled or not self.dir.exists():
            return 0
        stamp = float(now if now is not None else time.time())
        cutoff = stamp - self.days * 86400
        removed = 0
        for path in self.dir.glob("*.jsonl"):
            try:
                file_time = time.mktime(
                    time.strptime(path.stem, "%Y-%m-%d")
                )
            except ValueError:
                continue        # 不是我们命名的文件，别碰
            if file_time < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("删旧记录失败（%s）：%s", path, exc)
        if removed:
            logger.info("群消息记录清理了 %d 个过期文件（保留 %d 天）", removed, self.days)
        return removed

    def stats(self) -> Dict[str, Any]:
        """面板每 2 秒轮询一次，这里不能每次全量数行。

        文件只追加，所以「字节数没变」就等于「行数没变」——按 (大小) 缓存就够了，
        既准确又不贵。重启后第一次会数一遍，之后只有真写了新行才会重数。
        """
        today = self.path_for(time.time())
        lines = 0
        try:
            size = today.stat().st_size if today.exists() else -1
        except OSError:
            size = -1
        with self._lock:
            cached_size, cached_lines = self._count_cache
            if size >= 0 and size == cached_size:
                lines = cached_lines
            else:
                if size > 0:
                    try:
                        with today.open("rb") as fp:
                            lines = sum(1 for _ in fp)
                    except OSError:
                        lines = 0
                self._count_cache = (size, lines)
        files = len(list(self.dir.glob("*.jsonl"))) if self.dir.exists() else 0
        return {
            "enabled": self.enabled,
            "files": files,
            "today": lines,
            "days": self.days,
            "error": self.write_error,
        }

    def close(self) -> None:
        """进程退出前把计数清零（文件每次都是独立打开的，没有常驻句柄）。"""
        with self._lock:
            self._pending = 0
