# -*- coding: utf-8 -*-
"""回归：并发与队列（同会话串行、跨会话并行、指纹恰好一次）。

    .venv\\Scripts\\python.exe tests\\test_concurrency.py

为什么单独一个脚本：另外九个验的都是「单条消息处理得对不对」，这里验的是
「很多条同时来的时候还对不对」。后者才是这套东西真正会出事的地方
（Timer 线程 + 每会话锁 + 合并队列 + 指纹），但以前一行都没测。

只替换「模型」和「发送」：`Bot.__new__` 绕开构造（真 Bot 要微信、向量模型、
API Key，这里一样都不需要），`handle` / `_send` 换成假实现，
**队列、`_chat_lock`、`_seen`、合并窗口全是真代码**。

第 6 组压的是会话锁的淘汰：配额压到 2、60 个会话轮着来，逼它在「有线程
正用着」的时候做淘汰。旧写法（dict + 清掉未被持有的）在这里会重入，现在的
引用计数实现不会——这条用例就是那次修复的看门狗。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from app.bot import Bot, _Seen
from app.chat_locks import ChatLocks
from app.sender import Sender
from app.chat_policies import ChatPrefs
from app.panel.state import MonitorState
from _support import FAILS, check, line

CHAT1 = "wxid_alice"
CHAT2 = "wxid_bob"
GROUP = "12345678@chatroom"


class Cfg:
    """够用的配置壳：只实现 get("a.b.c", default)。"""

    def __init__(self, data: dict) -> None:
        self.data = data

    def get(self, path: str, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class Probe:
    """记录 handle 的进入/离开，用来判「同会话有没有真的并发」。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = []   # [(chat_id, query)]
        self.keys = []    # 每次 flush 带来的消息指纹

    def enter(self, chat_id: str, query: str) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((chat_id, query))

    def leave(self) -> None:
        with self._lock:
            self.active -= 1


def make_bot(work: Path, *, merge_window: float = 0.02) -> Bot:
    """拼一个只差模型的 Bot：队列/锁/指纹/状态都是真的。"""
    work.mkdir(parents=True, exist_ok=True)   # _Seen 落盘要用，必须先建
    bot = Bot.__new__(Bot)
    bot.cfg = Cfg({
        "wechat": {
            "reply_private": True,
            "group_at_only": False,       # 群聊一律 auto，免得被 @ 判定挡在门外
            "max_age_minutes": 0,         # 0 = 不做过期检查
            "merge_window": merge_window,
            "split_reply": False,
            "cooldown": 0,
            "typing_per_char": 0,
        },
        "people": {"self_names": []},
    })
    bot.chats = ChatPrefs(work / "chats.json")
    bot.people = None
    bot.glog = None
    bot.state = MonitorState()
    bot.bridge = None          # 没有接入层：_send_voice 直接返回，不碰微信
    bot.tts = None
    bot.me = ""
    bot._turns = {}
    bot._pending = {}
    bot._timers = {}
    bot.chat_locks = ChatLocks()
    bot.sender = Sender(bot.cfg, lambda: None)   # 没有接入层：语音/发送都直接返回
    bot._last_sent = {}
    bot._lock = threading.Lock()
    bot._seen = _Seen(work / "seen.json")
    return bot


def fake_handle(probe: Probe, delay: float = 0.0, fail_times: int = 0):
    """假 handle：只做「占住一会儿 + 记账」，签名与真的一致。"""
    left = {"n": fail_times}

    def handle(chat_id: str, user_text: str, chat_name: str = "",
               sender: str = "", sender_name: str = "") -> dict:
        probe.enter(chat_id, user_text)
        try:
            if left["n"] > 0:
                left["n"] -= 1
                raise RuntimeError("模拟处理失败")
            if delay:
                time.sleep(delay)
            return {"reply": f"回:{user_text}", "elapsed": 0.0,
                    "stats": {}, "tools": [], "voice": []}
        finally:
            probe.leave()

    return handle


def key_spy(probe: Probe):
    """顶掉 _record_key_reply：真实现要 glog，这里只把指纹收下来。"""

    def spy(chat_id, msg, reply, ok, keys):
        probe.keys.extend(keys)

    return spy


def msg(chat_id: str, text: str, *, sid: int, sender: str = "",
        sender_name: str = "") -> dict:
    return {
        "chat_id": chat_id,
        "chat_name": chat_id,
        "sender": sender,
        "sender_name": sender_name,
        "is_self": False,
        "type": "text",
        "content": text,
        "time": time.time(),
        "raw": {"local_id": sid},
    }


def wait_until(pred, timeout: float = 6.0, step: float = 0.01) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


# --------------------------------------------------------------------- 用例
def case_serial_same_chat(work: Path) -> None:
    line("1. 同一会话：消息比处理快，队列叠起来也只能一条一条跑")
    probe = Probe()
    bot = make_bot(work / "serial")
    bot.handle = fake_handle(probe, delay=0.08)
    bot._record_key_reply = key_spy(probe)

    total = 8
    for i in range(total):
        bot.on_message(msg(CHAT1, f"第{i}句", sid=i))
        time.sleep(0.02)      # 20ms 一条，而每条要 80ms 才处理完

    done = wait_until(lambda: len(probe.keys) >= total)
    check(f"{total} 条全部处理完", done and len(probe.keys) == total,
          f"{len(probe.keys)}/{total}")
    check("★ 同会话峰值并发 = 1（串行保证没破）", probe.max_active == 1,
          f"峰值 {probe.max_active}")
    check("每条消息恰好处理一次（无重复、无遗漏）",
          len(probe.keys) == len(set(probe.keys)), f"{len(probe.keys)} 条指纹")


def case_duplicate_key(work: Path) -> None:
    line("2. 同一条消息被多个线程同时投递：只认第一次")
    probe = Probe()
    bot = make_bot(work / "dup")
    bot.handle = fake_handle(probe)
    bot._record_key_reply = key_spy(probe)

    same = msg(CHAT1, "同一句话", sid=42)
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait()        # 尽量让 16 个线程同时进 on_message
        bot.on_message(same)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    wait_until(lambda: len(probe.keys) >= 1)
    time.sleep(0.15)          # 留出「错误地再处理一次」的时间

    check("16 次投递只处理 1 次", len(probe.keys) == 1 and len(probe.calls) == 1,
          f"handle {len(probe.calls)} 次 / 指纹 {len(probe.keys)} 个")


def case_parallel_chats(work: Path) -> None:
    line("3. 不同会话：必须并行，不能互相排队")
    probe = Probe()
    bot = make_bot(work / "parallel")
    delay = 0.5
    bot.handle = fake_handle(probe, delay=delay)
    bot._record_key_reply = key_spy(probe)

    started = time.time()
    t1 = threading.Thread(target=bot.on_message, args=(msg(CHAT1, "A 的问题", sid=1),))
    t2 = threading.Thread(target=bot.on_message, args=(msg(CHAT2, "B 的问题", sid=2),))
    t1.start()
    t2.start()
    wait_until(lambda: len(probe.keys) >= 2, timeout=8)
    elapsed = time.time() - started

    check("★ 两个会话并行处理（串行会是 %.1fs 以上）" % (delay * 2),
          elapsed < delay * 2 * 0.85, f"{elapsed:.2f}s")
    check("并发峰值 = 2", probe.max_active == 2, f"峰值 {probe.max_active}")


def case_group_split_by_sender(work: Path) -> None:
    line("4. 群里两个人同一窗口说话：各排各的，不许拼成一句")
    probe = Probe()
    bot = make_bot(work / "split", merge_window=0.15)
    bot.handle = fake_handle(probe)
    bot._record_key_reply = key_spy(probe)

    # 同一个人连说两句会被合并（这是合并窗口的本意），所以这里只看「跨人」。
    bot.on_message(msg(GROUP, "A 说的话", sid=1, sender="wxid_a", sender_name="A"))
    bot.on_message(msg(GROUP, "B 说的话", sid=2, sender="wxid_b", sender_name="B"))

    done = wait_until(lambda: len(probe.calls) >= 2)
    queries = [q for _, q in probe.calls]
    check("拆成两条独立 query", done and len(queries) == 2, str(queries))
    check("没有把两个人拼成一条", all(
        ("A 说的话" in q) != ("B 说的话" in q) for q in queries), str(queries))
    check("两条都归属同一个群", all(c == GROUP for c, _ in probe.calls),
          str([c for c, _ in probe.calls]))


def case_failure_rolls_back_seen(work: Path) -> None:
    line("5. 处理失败：指纹要撤销，重投还能再处理")
    probe = Probe()
    bot = make_bot(work / "retry")
    bot.handle = fake_handle(probe, fail_times=1)   # 第一次必失败
    bot._record_key_reply = key_spy(probe)

    m = msg(CHAT1, "会失败的一次", sid=7)
    bot.on_message(m)
    wait_until(lambda: bot.state.errors >= 1)
    check("第一次处理失败，错误计数 +1", bot.state.errors == 1, f"{bot.state.errors}")
    check("失败的那条没有记成「已处理」", len(probe.keys) == 0, f"{len(probe.keys)} 个指纹")

    bot.on_message(m)   # 同一 key 重新投递
    again = wait_until(lambda: len(probe.keys) >= 1)
    check("★ 重投后能再处理一次（指纹已撤销）", again and len(probe.keys) == 1,
          f"{len(probe.keys)} 个指纹 / errors={bot.state.errors}")


def case_lock_eviction(work: Path) -> None:
    line("6. 会话锁：配额很小、来回淘汰时，也不能把正在用的锁抽走")
    # 这条对应 bot.py 2026-10-29 前的写法：dict 缓存锁，超量就删「没被持有的」。
    # 删的时候看不见「已经拿到引用、还没 acquire」的线程，于是同一个会话能同时
    # 被两把锁放进去。这里把配额压到 2，让淘汰每次借还都发生，把窗口放大。
    locks = ChatLocks(limit=2)
    guard = threading.Lock()
    per_key: dict = {}
    bad = []

    def worker(index: int) -> None:
        for step in range(60):
            key = f"chat{index % 20}-{step % 3}"     # 60 个会话轮着来，远超配额
            with locks.hold(key):
                with guard:
                    per_key[key] = per_key.get(key, 0) + 1
                    if per_key[key] > 1:
                        bad.append(key)
                time.sleep(0.001)                    # 持有一小会儿，给别的线程机会
                with guard:
                    per_key[key] -= 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    check("★ 同一个会话从没被两个线程同时进入", not bad,
          f"{len(bad)} 次重入 {bad[:3]}")
    check("淘汰确实在发生（缓存没涨到几十个）", len(locks) <= 2, f"缓存 {len(locks)} 个")

    # 定点头：直接把「已经借到锁对象、但还没 acquire」这个状态造出来——它正是
    # 老写法看不见的窗口（`Lock.locked()` 那时是 False）。此时触发淘汰，
    # 新实现必须仍然把同一把锁还给我，否则同一个会话就有两把锁了。
    probe = ChatLocks(limit=1)
    held = probe._borrow("busy")
    for i in range(5):
        other = probe._borrow(f"other{i}")
        probe._give_back(f"other{i}", other)
    again = probe._borrow("busy")
    check("★ 已借出（未 acquire）的锁不会被淘汰", again is held,
          "同一把" if again is held else "被换成了新锁对象")
    probe._give_back("busy", held)
    probe._give_back("busy", again)


def main() -> int:
    import shutil
    import tempfile

    print("=" * 52)
    print(" 并发与队列回归（真实线程，临时目录）")
    print("=" * 52)
    work = Path(tempfile.mkdtemp(prefix="deepchat-test-concurrency-"))
    print(f"  数据目录：{work}")
    try:
        case_serial_same_chat(work)
        case_duplicate_key(work)
        case_parallel_chats(work)
        case_group_split_by_sender(work)
        case_failure_rolls_back_seen(work)
        case_lock_eviction(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("-" * 60)
    if FAILS:
        print(f"  共 {len(FAILS)} 项失败：")
        for name in FAILS:
            print(f"    - {name}")
        return 1
    print("  全部通过：同会话串行、跨会话并行、指纹恰好一次、失败可重投。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
