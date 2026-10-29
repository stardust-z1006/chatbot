# -*- coding: utf-8 -*-
"""运行状态收集：给后台面板看的实时数据。

全在内存里，只保留最近一小段：面板是「看现在怎么样」的东西，不是审计日志，
真正的历史在 data/bot.log 里。所有方法都在处理消息的线程里被调用，
所以每样东西都过同一把锁，读的时候拿一次快照就走。

三个环：
  - recent：最近 40 条消息（谁发的、多久回、调了什么工具、回了什么）
  - logs：最近 200 条日志（面板上直接滚，不用去翻文件）
  - latencies：最近 100 次耗时（算平均）

计数不分会话，因为 deepChat 本来就是单账号单进程；要看某个会话说了什么，看 recent。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional

RECENT_CAP = 40
LOG_CAP = 200
LATENCY_CAP = 100


@dataclass
class Record:
    """一条处理过的消息。"""

    at: float
    chat: str
    chat_name: str
    text: str
    reply: str
    elapsed: float
    tools: List[str]
    ok: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at,
            "time": time.strftime("%H:%M:%S", time.localtime(self.at)),
            "chat": self.chat,
            "chat_name": self.chat_name,
            "text": self.text[:120],
            "reply": self.reply[:160],
            "elapsed": round(self.elapsed, 2),
            "tools": self.tools,
            "ok": self.ok,
        }


class MonitorState:
    """计数器 + 三个环。线程安全：所有读写都拿 self._lock。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.running = False
        self.account = ""
        self.messages = 0
        self.replies = 0
        self.send_fail = 0
        self.errors = 0
        self.last_message_at = 0.0
        self.last_elapsed = 0.0
        self.tokens_last = 0
        self.tokens_budget = 0
        self._recent: Deque[Record] = deque(maxlen=RECENT_CAP)
        self._logs: Deque[Dict[str, Any]] = deque(maxlen=LOG_CAP)
        self._latencies: Deque[float] = deque(maxlen=LATENCY_CAP)
        self._tools: Dict[str, Dict[str, float]] = {}
        # 接话打分的命中/漏掉计数 + 按原因分布（最多 8 个 key，防无限涨）
        self._engage_hit = 0
        self._engage_skip = 0
        self._engage_reasons: Dict[str, int] = {}

    # ------------------------------------------------------------ 写
    def message(
        self,
        chat: str,
        chat_name: str,
        text: str,
        reply: str,
        elapsed: float,
        tools: Optional[List[str]] = None,
        ok: bool = True,
    ) -> None:
        with self._lock:
            self.messages += 1
            self.last_message_at = time.time()
            self.last_elapsed = elapsed
            self._latencies.append(elapsed)
            self._recent.append(
                Record(
                    at=self.last_message_at,
                    chat=chat,
                    chat_name=chat_name or chat,
                    text=text or "",
                    reply=reply or "",
                    elapsed=elapsed,
                    tools=list(tools or []),
                    ok=ok,
                )
            )

    def reply_sent(self, ok: bool) -> None:
        with self._lock:
            self.replies += 1
            if not ok:
                self.send_fail += 1

    def error(self) -> None:
        with self._lock:
            self.errors += 1

    def tool(self, name: str, ms: float, ok: bool = True) -> None:
        with self._lock:
            item = self._tools.setdefault(name, {"count": 0, "fail": 0, "total_ms": 0.0})
            item["count"] += 1
            item["total_ms"] += ms
            if not ok:
                item["fail"] += 1

    def tokens(self, used: int, budget: int) -> None:
        with self._lock:
            self.tokens_last = int(used)
            self.tokens_budget = int(budget)

    def engage(self, hit: bool, reasons: List[str]) -> None:
        """接话打分结果：命中/漏掉计数 + 按原因计数（原因最多记 8 个）。"""
        with self._lock:
            if hit:
                self._engage_hit += 1
            else:
                self._engage_skip += 1
            for reason in list(reasons or [])[:8]:
                self._engage_reasons[reason] = self._engage_reasons.get(reason, 0) + 1

    def log_record(self, level: str, name: str, msg: str) -> None:
        with self._lock:
            self._logs.append(
                {
                    "at": time.time(),
                    "time": time.strftime("%H:%M:%S"),
                    "level": level,
                    "name": name,
                    "msg": msg,
                }
            )

    def clear_recent(self) -> None:
        with self._lock:
            self._recent.clear()

    # ------------------------------------------------------------ 读
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            latencies = list(self._latencies)
            avg = sum(latencies) / len(latencies) if latencies else 0.0
            tools = {
                name: {
                    "count": int(item["count"]),
                    "fail": int(item["fail"]),
                    "avg_ms": round(item["total_ms"] / item["count"], 1)
                    if item["count"]
                    else 0.0,
                }
                for name, item in self._tools.items()
            }
            return {
                "running": self.running,
                "account": self.account,
                "started_at": self.started_at,
                "uptime": round(time.time() - self.started_at, 1),
                "messages": self.messages,
                "replies": self.replies,
                "send_fail": self.send_fail,
                "errors": self.errors,
                "last_message_at": self.last_message_at,
                "last_elapsed": round(self.last_elapsed, 2),
                "avg_elapsed": round(avg, 2),
                "tokens": {"last": self.tokens_last, "budget": self.tokens_budget},
                "engage": {
                    "hit": self._engage_hit,
                    "skip": self._engage_skip,
                    "by_reason": dict(self._engage_reasons),
                },
                "tools": tools,
                "recent": [r.as_dict() for r in reversed(self._recent)],
                "logs": list(self._logs)[-120:],
                "now": time.time(),
            }


class StateLogHandler(logging.Handler):
    """把日志塞进环里，面板直接读，不用去 tail 文件。

    只收 INFO 及以上，DEBUG 太吵（面板滚不完）。
    """

    def __init__(self, state: MonitorState, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.state = state
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.state.log_record(
                record.levelname, record.name, record.getMessage()
            )
        except Exception:  # noqa: BLE001
            self.handleError(record)


def attach_log_handler(state: MonitorState) -> StateLogHandler:
    """挂到 root 上。

    通过 `logging_setup.register_extra` 挂，而不是直接 addHandler：
    wechatauto 导入时会 clear 掉 root 的全部 handler，注册过的会在
    下一次 setup()/ensure() 里被自动补回来，否则面板日志环会静默变空。
    """
    from ..logging_setup import register_extra

    handler = StateLogHandler(state)
    register_extra(handler)
    return handler


def detach_log_handler(handler: Optional[StateLogHandler]) -> None:
    """摘掉日志环（进程退出、面板收起时用）。"""
    if handler is None:
        return
    from ..logging_setup import unregister_extra

    unregister_extra(handler)
