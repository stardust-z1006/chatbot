# -*- coding: utf-8 -*-
"""按会话发锁：同会话串行、不同会话并行。

为什么单独一个东西：机器人同时跑着很多 Timer 线程（每条消息一个合并窗口），
同一个会话必须一条一条处理，否则 `_turns` 会交错、回复顺序会乱、甚至同一条
消息答两遍。这个保证全靠「同一个 chat_id 永远拿到同一把锁」。

**为什么不用 dict + 「超了就清没被持有的」**（2026-10-29 前的写法）：
`threading.Lock` 只告诉你「有没有人持着」，不告诉你「有没有人已经拿到引用、
还没 acquire」。后者是一个真实存在的窗口：

    T1: lock = _chat_lock("群A")      # 拿到对象，还没 acquire
    T2: 触发清理，看到它没被持有 → 删掉，重建一把新锁
    T1: 在旧锁上 acquire            T2: 在新锁上 acquire
    → 同一个群，两个线程同时进去了

窗口极窄、要几百个会话才触发，但一旦触发，串行保证就是静默失效——查不出来。
所以这里改成**引用计数**：只要还有线程在持有或在排队，这条记录绝不删。
淘汰只发生在「引用归零」的那一刻，而那一刻只可能由最后一个离开的线程触发，
它自己删自己是安全的。
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any, Dict, Iterator, List


class ChatLocks:
    """按 key 发锁，引用归零且超量时才淘汰。"""

    def __init__(self, limit: int = 512) -> None:
        self._limit = max(1, int(limit))
        self._guard = threading.Lock()
        # key -> [锁, 引用数]。用 list 而不是小对象：改动只在原地，不需要再分配。
        self._entries: Dict[str, List[Any]] = {}

    @contextlib.contextmanager
    def hold(self, key: str) -> Iterator[None]:
        """独占这个 key 对应的会话。with 块内同 key 串行，不同 key 并行。"""
        entry = self._borrow(key)
        lock = entry[0]
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            self._give_back(key, entry)

    def _borrow(self, key: str) -> List[Any]:
        """取（或建）一条记录并**先把引用数加上**，再做任何可能阻塞的事。

        顺序不能反：先 acquire 再加引用，等于把「还没算作有人用」的锁暴露给
        清理逻辑，就退回到老写法那个窗口了。
        """
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = [threading.Lock(), 0]
                self._entries[key] = entry
            entry[1] += 1
            return entry

    def _give_back(self, key: str, entry: List[Any]) -> None:
        with self._guard:
            entry[1] -= 1
            # 配额只在「没人用」的时刻检查：这样删除一定发生在最后一个线程
            # 离开之后，不会把别人手里的锁抽走。
            if entry[1] > 0 or len(self._entries) <= self._limit:
                return
            if self._entries.get(key) is entry:
                del self._entries[key]

    def __len__(self) -> int:
        """当前缓存了多少个会话的锁（面板/测试用）。"""
        with self._guard:
            return len(self._entries)
