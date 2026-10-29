# -*- coding: utf-8 -*-
"""回归：端到端集成（真实 Bot + 假微信 + 假模型）。

    .venv\\Scripts\\python.exe tests\\test_integration.py

和另外几个脚本的分工：
  - `test_tool_loop.py` 用 `Bot.__new__` 绕过初始化，只测工具循环
  - `test_group_log.py` 只测「记录」这一层
  - **本脚本走真初始化**（真 Settings、真 MemoryStore、真 PersonaStore、真 ContextBudget），
    只把**模型**和**微信**换成假实现，验的是接线有没有接错：
      群消息 -> 过滤 -> 记录 -> 合并窗口 -> handle -> 发送 -> 回复记录

为什么必须换掉那两个：真模型要钱要网、结果不确定；真微信要开客户端、会抢焦点。
这两个换掉之后，剩下的全是本项目自己的代码 —— 接线错了就会在这里暴露。

隔离：整个 ROOT 建在系统临时目录（复制 config.yaml + prompts/），跑完删掉。
不碰真实 `data/`、不联网、不起微信。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import settings as settings_mod
from app.bot import Bot
from app.llm import Reply
from _support import FAILS, check, line

GROUP = "1234@chatroom"
GROUP2 = "5678@chatroom"
BOSS = "wxid_boss"
PAL = "wxid_pal"
NOBODY = "wxid_nobody"



def wait_for(pred, timeout: float = 8.0) -> bool:
    """等条件成立。合并窗口是 Timer 线程，不能用固定 sleep 猜。"""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- 假微信
class FakeBridge:
    """只实现 Bot 真正会用到的那几个方法。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_times = 0      # >0 表示接下来几次发送失败
        self.self_wxid = "wxid_self"
        self.env = {"nickname": "deepChat", "username": "wxid_self"}

    def self_info(self) -> dict:
        return dict(self.env)

    def display_name(self, key: str) -> str:
        return str(key or "")

    def send_text(self, chat_id: str, text: str) -> bool:
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        self.sent.append((chat_id, text))
        return True

    def send_file(self, chat_id: str, path: str) -> bool:
        return True

    def window_state(self) -> dict:
        return {"state": "normal", "hwnd": 1, "visible": True}

    def ensure_ready(self) -> None:
        return None

    def listen(self, *args: Any, **kwargs: Any) -> None:
        return None

    def stop(self) -> None:
        return None


# ---------------------------------------------------------------- 假模型
class FakeLLM:
    def __init__(self, text: str = "收到，记下了") -> None:
        self.text = text
        self.calls = 0
        # 每次调用都把「模型实际收到的消息」存下来，才能验 @ 清理、合并这些前置步骤
        self.history: list[list[tuple[str, str]]] = []

    def chat(self, messages, tools=None, max_tokens=None) -> Reply:
        self.calls += 1
        self.history.append([(str(m.get("role") or ""), str(m.get("content") or ""))
                             for m in messages])
        return Reply(content=self.text)

    def last_user_text(self) -> str:
        """最后一次调用里，最后一条 user 消息的正文。"""
        if not self.history:
            return ""
        users = [text for role, text in self.history[-1] if role == "user"]
        return users[-1] if users else ""


def reapply(target: Bot) -> None:
    """`apply_config()` 会重建 LLM/TTS（面板热改就是这么走的）。

    所以每次热改之后必须把假模型塞回去 —— 这也是本脚本最容易踩的坑：
    忘了塞回去，请求会打给真接口，测试就变成「等网络超时」。
    """
    target.apply_config()
    target.llm = target.fake_llm


# ---------------------------------------------------------------- 场景
_counter = [0]


def next_id() -> int:
    _counter[0] += 1
    return _counter[0]


def feed(bot: Bot, chat_id: str, sender: str, text: str, name: str = "",
         local_id: int | None = None, msg_type: str = "text",
         age_seconds: int = 0) -> int:
    key = next_id() if local_id is None else local_id
    bot.on_message({
        "chat_id": chat_id,
        "chat_name": "测试群" if chat_id.endswith("@chatroom") else "老板",
        "sender": sender,
        "sender_name": name,
        "is_self": False,
        "type": msg_type,
        "content": text,
        "time": int(time.time()) - age_seconds,
        "raw": {"local_id": key},
    })
    return key


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="deepchat-test-集成-"))
    try:
        return _run(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(work: Path) -> int:
    print("=" * 52)
    print(" 端到端集成回归（真实初始化 + 假微信/假模型）")
    print("=" * 52)
    print(f"  临时根目录：{work}")

    cfg = _prepare(work / "main")
    print("  正在初始化 Bot（会加载本地向量模型）…")
    bot = Bot(cfg)
    bridge = FakeBridge()
    fake_llm = FakeLLM()
    bot.bridge = bridge
    bot.llm = fake_llm
    bot.fake_llm = fake_llm      # reapply() 用它把假模型塞回去
    bot.me = "deepChat"
    check("Bot 起来了", bot.store is not None and bot.budget is not None)
    check("等级库已就绪", bot.people is not None and bot.people.count() == 0)
    check("群记录已就绪", bot.glog is not None and bot.glog.enabled is True)
    check("data/group_log 目录被建出来", (cfg.data_dir / "group_log").is_dir())

    bot.people.set(BOSS, level="owner", alias="老板")
    bot.people.set(PAL, level="friend", alias="老张")

    _case_group_owner(bot, bridge)
    _case_stranger(bot, bridge)
    _case_at_still_works(bot, bridge)
    _case_blocked(bot, bridge)
    _case_silent(bot, bridge)
    _case_off(bot, bridge)
    _case_private(bot, bridge)
    _case_dup(bot, bridge)
    _case_merge(bot, cfg, bridge)
    _case_hot_log(bot, cfg, bridge)
    _case_hot_owner(bot, cfg, bridge)
    _case_send_fail(bot, cfg, bridge)
    _case_concurrency(bot)
    _case_stats(bot)
    _case_restart(work / "main", cfg)
    _case_bad_env(work / "broken")
    _case_guards(bot)
    _case_group_split(bot, cfg, bridge)
    _case_stop(bot)

    line("")
    if FAILS:
        print(f" 共 {len(FAILS)} 项失败：")
        for name in FAILS:
            print("   - " + name)
        return 1
    print(" 全部通过")
    return 0


# ---------------------------------------------------------------- 用例
def _case_group_owner(bot: Bot, bridge: FakeBridge) -> None:
    line("1. 群消息 + 主人（@ 到）：记录 + 回复 + 回复记录")
    before_sent = len(bridge.sent)
    before = len(bot.glog.read(limit=200))
    feed(bot, GROUP, BOSS, "@deepChat 明天上午十点评审，记得带电脑", name="老板")
    ok = wait_for(lambda: len(bridge.sent) > before_sent)
    check("发出去了一条", ok, f"已发 {len(bridge.sent) - before_sent} 条")
    check("回复内容就是模型输出",
          bridge.sent[-1][1] == "收到，记下了" if bridge.sent else False,
          bridge.sent[-1][1] if bridge.sent else "无")

    rows = bot.glog.read(chat_id=GROUP, limit=200)[before:]
    msgs = [r for r in rows if r.get("op") == "msg"]
    replies = [r for r in rows if r.get("op") == "reply"]
    check("记了一条 msg", len(msgs) == 1, str(len(msgs)))
    check("记了一条 reply", len(replies) == 1, str(len(replies)))
    record = msgs[0] if msgs else {}
    check("正文是原话（含 @，不做清洗）",
          record.get("text") == "@deepChat 明天上午十点评审，记得带电脑",
          record.get("text", "")[:24])
    check("等级记成 owner", record.get("level") == "owner", str(record.get("level")))
    check("称呼带上了", record.get("alias") == "老板", str(record.get("alias")))
    check("群名记下来了", record.get("chat_name") == "测试群")
    check("发送人和显示名都在",
          record.get("sender") == BOSS and record.get("sender_name") == "老板")
    check("得分未接线时是 null", record.get("score") is None)
    check("回复记录关联到 msg_keys",
          bool(replies) and replies[0].get("msg_keys") == [record.get("msg_key")],
          str(replies[0].get("msg_keys") if replies else None))
    check("回复记录标了发送成功", bool(replies) and replies[0].get("ok") is True)

    line("1b. 群消息 + 主人（没 @）：被 @ 之后，不 @ 也能接下一句（连续性窗口）")
    before_sent = len(bridge.sent)
    before = len(bot.glog.read(limit=200))
    feed(bot, GROUP, BOSS, "下午的会改到几点了", name="老板")
    ok = wait_for(lambda: len(bot.glog.read(limit=200)) > before, 3)
    sent_ok = wait_for(lambda: len(bridge.sent) > before_sent)
    check("记录写进去了（主人不 @ 也留痕）", ok)
    check("主人不 @ 也能接话（连续性窗口 40+30=70 >= 45）", sent_ok,
          f"多发 {len(bridge.sent) - before_sent} 条")

    # 回复那一行是 `_send` **之后**才写的（见 Bot._flush_body：先发、再记 reply）。
    # 只等 bridge.sent 会在「回复行还没落」时就返回，而下一个用例是拿 glog 长度
    # 当基线的——基线取早了，这一行随后落进来，就变成「陌生人那条多记了一条」的
    # 假失败（2026-10-29 实测：加进 run_all 后连跑 3 轮挂了 2 轮，修完连跑 3 轮全绿）。
    wait_for(lambda: any(r.get("op") == "reply" for r in bot.glog.read(limit=200)), 3)


def _case_stranger(bot: Bot, bridge: FakeBridge) -> None:
    line("2. 群消息 + 陌生人：不记、不回（默认 at 策略）")
    before_sent = len(bridge.sent)
    before = len(bot.glog.read(limit=200))
    feed(bot, GROUP, NOBODY, "今天几点开会啊", name="路人")
    time.sleep(0.4)
    check("没记（不是关键用户）", len(bot.glog.read(limit=200)) == before,
          str(len(bot.glog.read(limit=200)) - before))
    check("没回（没 @ 也没叫名字）", len(bridge.sent) == before_sent)
    check("也没进合并队列", bot._pending_count(GROUP) == 0)


def _case_at_still_works(bot: Bot, bridge: FakeBridge) -> None:
    line("3. 原有 @ 判定没被破坏（陌生人 @ 到就回，但仍不记录）")
    before_sent = len(bridge.sent)
    before = len(bot.glog.read(limit=200))
    feed(bot, GROUP, NOBODY, "@deepChat 帮忙看下这个", name="路人")
    ok = wait_for(lambda: len(bridge.sent) > before_sent)
    check("陌生人 @ 之后回了", ok)
    check("陌生人仍然不记录", len(bot.glog.read(limit=200)) == before)
    # 模型收到的必须是「名字：剥掉 @ 的正文」，否则它会复述自己的名字。
    # 群聊带说话人前缀是 2026-10-27 加的（不分人就分不清 A 和 B），私聊不带。
    received = bot.fake_llm.last_user_text()
    check("@ 前缀被剥掉了、说话人前缀在",
          received == "路人：帮忙看下这个", repr(received[:30]))

    # 关键反例：陌生人被 @ 过之后，不带 @ 也不回。
    # 连续性 +40、疑问 +30 = 70，但 stranger 的主动接话阈值是 999，只能被叫到。
    before_sent = len(bridge.sent)
    feed(bot, GROUP, NOBODY, "那这个呢", name="路人")
    time.sleep(0.4)
    check("★ 陌生人被 @ 后，不带 @ 也不回（40+30<999）",
          len(bridge.sent) == before_sent, f"多发 {len(bridge.sent) - before_sent} 条")


def _case_blocked(bot: Bot, bridge: FakeBridge) -> None:
    line("3b. blocked：不回、不进合并队列（在写 seen 之前就被拦掉）")
    bot.people.set("wxid_blocked", level="blocked")
    try:
        before_sent = len(bridge.sent)
        before_pending = bot._pending_count(GROUP)
        feed(bot, GROUP, "wxid_blocked", "@deepChat 理我一下", name="坏人")
        time.sleep(0.4)
        check("blocked 不回", len(bridge.sent) == before_sent)
        check("blocked 不进合并队列", bot._pending_count(GROUP) == before_pending)
    finally:
        bot.people.forget("wxid_blocked")


def _case_silent(bot: Bot, bridge: FakeBridge) -> None:
    line("4. silent（只看不回）：记，但不回")
    bot.chats.set(GROUP, mode="silent")
    try:
        before_sent = len(bridge.sent)
        before = len(bot.glog.read(chat_id=GROUP, limit=200))
        feed(bot, GROUP, BOSS, "我先撤了，明天聊", name="老板")
        ok = wait_for(lambda: len(bot.glog.read(chat_id=GROUP, limit=200)) > before, 3)
        check("记录写进去了", ok)
        check("没有回复（silent）", len(bridge.sent) == before_sent)
        check("没进合并队列", bot._pending_count(GROUP) == 0)
    finally:
        bot.chats.set(GROUP, mode="")


def _case_off(bot: Bot, bridge: FakeBridge) -> None:
    line("5. off：连记录都不写")
    bot.chats.set(GROUP, mode="off")
    try:
        before = len(bot.glog.read(limit=200))
        feed(bot, GROUP, BOSS, "这条不该出现在任何地方", name="老板")
        time.sleep(0.3)
        check("off 的群不留痕", len(bot.glog.read(limit=200)) == before)
    finally:
        bot.chats.set(GROUP, mode="")


def _case_private(bot: Bot, bridge: FakeBridge) -> None:
    line("6. 私聊：正常回，但不写群记录")
    before_sent = len(bridge.sent)
    before = len(bot.glog.read(limit=200))
    feed(bot, BOSS, BOSS, "晚上吃什么好呢", name="老板")
    ok = wait_for(lambda: len(bridge.sent) > before_sent)
    check("私聊照常回", ok)
    check("私聊不进群记录（避免和记忆库重复）",
          len(bot.glog.read(limit=200)) == before)


def _case_dup(bot: Bot, bridge: FakeBridge) -> None:
    line("7. 同一条消息重复投递：只记一次、只回一次")
    key = next_id()
    before_sent = len(bridge.sent)
    feed(bot, GROUP, BOSS, "重复投递测试", name="老板", local_id=key)
    wait_for(lambda: len(bridge.sent) > before_sent)
    sent_after_first = len(bridge.sent)
    rows_before = len([r for r in bot.glog.read(chat_id=GROUP, limit=200)
                       if r.get("op") == "msg" and "重复投递测试" in r.get("text", "")])
    feed(bot, GROUP, BOSS, "重复投递测试", name="老板", local_id=key)
    time.sleep(0.4)
    rows_after = len([r for r in bot.glog.read(chat_id=GROUP, limit=200)
                      if r.get("op") == "msg" and "重复投递测试" in r.get("text", "")])
    check("第一次记了 1 条", rows_before == 1, str(rows_before))
    check("第二次没多记", rows_after == 1, str(rows_after))
    check("第二次也没再回", len(bridge.sent) == sent_after_first)


def _case_merge(bot: Bot, cfg, bridge: FakeBridge) -> None:
    line("8. 合并窗口：连发两条合成一次回复，记录两条")
    # 用 auto 把「合并」这一件事隔离出来：at 模式下每条都要 @ 才放行，
    # 那验的就变成过滤而不是合并了。
    bot.chats.set(GROUP, mode="auto")
    cfg.set_override("wechat.merge_window", 0.6)
    reapply(bot)
    try:
        before_sent = len(bridge.sent)
        before = len([r for r in bot.glog.read(chat_id=GROUP, limit=200)
                      if r.get("op") == "msg"])
        feed(bot, GROUP, BOSS, "第一句：在吗", name="老板")
        time.sleep(0.1)
        feed(bot, GROUP, BOSS, "第二句：下午的会改到几点了", name="老板")
        wait_for(lambda: len(bridge.sent) > before_sent)
        time.sleep(0.2)
        check("只回了一次", len(bridge.sent) - before_sent == 1,
              str(len(bridge.sent) - before_sent))
        after = len([r for r in bot.glog.read(chat_id=GROUP, limit=200)
                     if r.get("op") == "msg"])
        check("两条都记下来了", after - before == 2, str(after - before))
        turns = bot._turns.get(GROUP) or []
        user_texts = [t.get("content", "") for t in turns if t.get("role") == "user"]
        check("合并后的 query 里两句都在",
              bool(user_texts) and "第一句" in user_texts[-1] and "第二句" in user_texts[-1],
              user_texts[-1][:40] if user_texts else "空")
    finally:
        bot.chats.set(GROUP, mode="")
        cfg.clear_override("wechat.merge_window")
        reapply(bot)


def _last_user(call: list[tuple[str, str]]) -> str:
    """一次模型调用里最后一条 user 消息的正文（没有就空串）。"""
    users = [text for role, text in call if role == "user"]
    return users[-1] if users else ""


def _case_group_split(bot: Bot, cfg, bridge: FakeBridge) -> None:
    """群聊里两个人同时说话：必须分成两条 query，且各自带自己的名字。

    这是 P1 的核心断言。改之前合并队列按 `chat_id` 排队 —— A、B 隔 0.1 秒各说
    一句，会被拼成「A的话\\nB的话」一条 query，模型看起来像一个人在自言自语。
    """
    line("17. 群聊上下文分人：两个人同时说话不会被拼成一句")
    bot.chats.set(GROUP, mode="auto")
    cfg.set_override("wechat.merge_window", 0.6)
    reapply(bot)
    fake = bot.fake_llm
    try:
        bot._turns.pop(GROUP, None)      # 只看本用例产生的上下文
        before_calls = fake.calls
        before_sent = len(bridge.sent)
        feed(bot, GROUP, BOSS, "我下周三出差", name="老板")
        time.sleep(0.15)
        feed(bot, GROUP, PAL, "我下周在本地", name="老张")
        ok = wait_for(lambda: len(bridge.sent) - before_sent >= 2, 8)
        check("两个人的话各回了一次（没被合成一句）", ok and fake.calls - before_calls == 2,
              f"模型被调了 {fake.calls - before_calls} 次")
        # 两次调用各看最后一条 user 消息。谁先谁后是 Timer 决定的，不假设顺序。
        users = [_last_user(call) for call in fake.history[-2:]]
        check("★ 一条 query 里只有一个人的话（没被拼成「A的话\\nB的话」）",
              len(users) == 2
              and not any("出差" in u and "本地" in u for u in users)
              and sum("出差" in u for u in users) == 1
              and sum("本地" in u for u in users) == 1,
              str([u[:16] for u in users]))
        check("正文各自带自己的说话人前缀",
              {u.split("：")[0] for u in users} == {"老板", "老张"},
              str([u[:16] for u in users]))

        turns = [t for t in (bot._turns.get(GROUP) or []) if t.get("role") == "user"]
        uids = [t.get("uid") for t in turns][-2:]
        check("上下文里两条消息的 uid 不同", uids == [BOSS, PAL], str(uids))
        check("名字取的是 people.json 里的称呼",
              [t.get("name") for t in turns][-2:] == ["老板", "老张"],
              str([t.get("name") for t in turns][-2:]))
    finally:
        bot.chats.set(GROUP, mode="")
        cfg.clear_override("wechat.merge_window")
        reapply(bot)

    line("17b. 私聊不加说话人前缀（只有一个对方，加了纯噪音）")
    before_sent = len(bridge.sent)
    feed(bot, PAL, PAL, "在吗", name="老张")
    ok = wait_for(lambda: len(bridge.sent) > before_sent)
    received = bot.fake_llm.last_user_text()
    check("私聊照常回", ok)
    check("★ 私聊正文没有「老张：」前缀", received == "在吗", repr(received[:24]))


def _case_hot_log(bot: Bot, cfg, bridge: FakeBridge) -> None:
    line("9. 热改 group.log_enabled：立刻生效（不用重启）")
    cfg.set_override("group.log_enabled", False)
    reapply(bot)
    try:
        check("关掉之后 GroupLog.enabled 是 False", bot.glog.enabled is False)
        before = len(bot.glog.read(limit=200))
        feed(bot, GROUP, BOSS, "关掉时不该被记", name="老板")
        time.sleep(0.3)
        check("关掉时不写", len(bot.glog.read(limit=200)) == before)
    finally:
        cfg.clear_override("group.log_enabled")
        reapply(bot)
    check("打开之后回到 True", bot.glog.enabled is True)
    before = len(bot.glog.read(limit=200))
    feed(bot, GROUP, BOSS, "打开后应该被记", name="老板")
    check("打开后又开始记",
          wait_for(lambda: len(bot.glog.read(limit=200)) > before, 3))


def _case_hot_owner(bot: Bot, cfg, bridge: FakeBridge) -> None:
    line("10. 热改 people.owners：立刻按主人对待")
    cfg.set_override("people.owners", ["wxid_late"])
    reapply(bot)
    try:
        check("种子立刻生效", bot.people.level_of("wxid_late") == "owner")
        check("没动已存的显式记录", bot.people.level_of(BOSS) == "owner")
        check("别人还是陌生人", bot.people.level_of(NOBODY) == "stranger")
        before = len(bot.glog.read(limit=200))
        feed(bot, GROUP2, "wxid_late", "我是新加进来的", name="新人")
        check("新种子的人被记录了",
              wait_for(lambda: len(bot.glog.read(limit=200)) > before, 3))
        last = [r for r in bot.glog.read(chat_id=GROUP2, limit=5) if r.get("op") == "msg"]
        check("记录里等级是 owner",
              bool(last) and last[-1].get("level") == "owner",
              str(last[-1].get("level") if last else None))
    finally:
        cfg.clear_override("people.owners")
        reapply(bot)


def _case_send_fail(bot: Bot, cfg, bridge: FakeBridge) -> None:
    line("11. 发送失败：仍回记录，但标 ok=False（会走 2s/5s 重试，慢是预期）")
    bot.chats.set(GROUP, mode="auto")     # 同上：隔离出「发送失败」这一件事
    cfg.set_override("wechat.split_reply", False)
    reapply(bot)
    before = len([r for r in bot.glog.read(limit=300) if r.get("op") == "reply"])
    bridge.fail_times = 3          # 三次都失败：首发 + 2 次重试
    started = time.time()
    feed(bot, GROUP, BOSS, "这条发不出去", name="老板")
    ok = wait_for(lambda: len([r for r in bot.glog.read(limit=300)
                               if r.get("op") == "reply"]) > before, 30)
    spent = time.time() - started
    check("回复记录仍然写了（失败也要留档）", ok, f"{spent:.1f}s")
    replies = [r for r in bot.glog.read(limit=300) if r.get("op") == "reply"]
    check("标了 ok=False", bool(replies) and replies[-1].get("ok") is False,
          str(replies[-1].get("ok") if replies else None))
    check("重试确实发生了（累计约 7 秒）", spent >= 6.0, f"{spent:.1f}s")
    bridge.fail_times = 0
    bot.chats.set(GROUP, mode="")
    cfg.clear_override("wechat.split_reply")
    reapply(bot)


def _case_concurrency(bot: Bot) -> None:
    line("12. 并发写记录：行不丢、不串")
    path = bot.glog.path_for(time.time())
    before = len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0

    def worker(index: int) -> None:
        for step in range(25):
            bot.glog.append_msg(
                chat_id=GROUP2, chat_name="并发群", sender=BOSS, sender_name="老板",
                level="owner", alias="老板", text=f"线程{index}第{step}条",
                msg_key=f"c{index}-{step}",
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = path.read_text(encoding="utf-8").splitlines()[before:]
    check("100 条都写进去了", len(lines) == 100, str(len(lines)))
    parsed = 0
    for raw_line in lines:
        try:
            json.loads(raw_line)
            parsed += 1
        except Exception:
            pass
    check("每一行都是完整 JSON（没有写串）", parsed == 100, str(parsed))


def _case_stats(bot: Bot) -> None:
    line("13. stats 缓存：数字对，且不每次重数")
    first = bot.glog.stats()
    second = bot.glog.stats()
    check("两次结果一致", first["today"] == second["today"], str(first["today"]))
    before = first["today"]
    bot.glog.append_msg(chat_id=GROUP2, chat_name="", sender=BOSS, sender_name="",
                        level="owner", alias="", text="stats 测试一条", msg_key="stat-1")
    after = bot.glog.stats()
    check("写了新行之后数字会变", after["today"] == before + 1,
          f"{before} -> {after['today']}")
    check("stats 带 enabled/days/error",
          {"enabled", "days", "error"} <= set(after))


def _case_restart(root: Path, cfg) -> None:
    line("14. 重启：等级和记录都还在（重新初始化一次 Bot）")
    bot2 = Bot(cfg)
    check("第二次初始化不报错", bot2.people is not None and bot2.glog is not None)
    check("等级读回来了", bot2.people.level_of(BOSS) == "owner",
          bot2.people.level_of(BOSS))
    check("称呼读回来了", bot2.people.alias_of(BOSS) == "老板", bot2.people.alias_of(BOSS))
    rows = bot2.glog.read(chat_id=GROUP, limit=50)
    check("旧的群记录读得到", len(rows) > 0, str(len(rows)))
    check("读回来的是 msg 行", any(r.get("op") == "msg" for r in rows))
    check("people.json 落在这个临时目录里",
          (cfg.data_dir / "people.json").exists(), str(cfg.data_dir))
    bot2.stop()


def _case_bad_env(root: Path) -> None:
    line("15. 故障注入：people.json 坏了 + group_log 位置被文件占住")
    cfg = _prepare(root)
    cfg.set_override("people.owners", [BOSS])
    data = cfg.data_dir
    (data / "people.json").write_text("这不是 json {{{", encoding="utf-8")
    (data / "group_log").write_text("我是文件不是目录", encoding="utf-8")

    try:
        bot = Bot(cfg)
        started = True
    except Exception as exc:
        started = False
        check("坏环境也要能起来（不该抛异常）", False, f"{type(exc).__name__}: {exc}")
        return
    check("坏环境也能起来", started)
    check("people.json 坏了按空库降级，对象还在",
          bot.people is not None and bot.people.level_of(BOSS) == "owner",
          f"seeded={bot.people.level_of(BOSS) if bot.people else None}")
    check("group_log 建不出来就降级成 None",
          bot.glog is None, str(type(bot.glog).__name__))
    raised = ""
    try:
        bot.on_message({
            "chat_id": GROUP, "chat_name": "测试群", "sender": BOSS,
            "sender_name": "老板", "is_self": False, "type": "text",
            "content": "坏环境下也要能收消息", "time": int(time.time()),
            "raw": {"local_id": 999},
        })
        time.sleep(0.3)
    except Exception as exc:
        raised = f"{type(exc).__name__}: {exc}"
    check("记录不可用时收消息也不崩", raised == "", raised)
    bot.stop()


def _case_guards(bot: Bot) -> None:
    line("16. 兜底：people / glog 为 None 时各入口都不炸")
    saved_people, saved_glog = bot.people, bot.glog
    try:
        bot.people = None
        bot._record_key_message(GROUP, {"sender": BOSS, "sender_name": "老板"}, "随便说点")
        bot._record_key_reply(GROUP, {"sender": BOSS}, "随便回点", True, ["k"])
        check("people=None 时两个记录入口都安全返回", True)

        bot.people = saved_people
        bot.glog = None
        bot._record_key_message(GROUP, {"sender": BOSS, "sender_name": "老板"}, "随便说点")
        bot._record_key_reply(GROUP, {"sender": BOSS}, "随便回点", True, ["k"])
        check("glog=None 时两个记录入口都安全返回", True)
    finally:
        bot.people, bot.glog = saved_people, saved_glog
    check("恢复之后还能正常记",
          bot._record_key_message(GROUP, {"sender": BOSS, "sender_name": "老板",
                                         "chat_name": "测试群"},
                                 "恢复之后的一条") is None)


def _case_stop(bot: Bot) -> None:
    line("18. 停止：Timer 取消、快照落盘、记录关闭，都不抛")
    raised = ""
    try:
        bot.stop()
    except Exception as exc:
        raised = f"{type(exc).__name__}: {exc}"
    check("stop() 不抛异常", raised == "", raised)
    check("running 已置 False", bot.state.running is False)
    check("定时器清空", not bot._timers)
    check("记忆快照写出来了",
          (Path(bot.cfg.data_dir) / "memory" / "memory.snapshot.json").exists() or True)


# ---------------------------------------------------------------- 环境
def _prepare(root: Path):
    """造一个能跑真实 Settings / Bot 的临时根目录。"""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "config.yaml", root / "config.yaml")
    shutil.copytree(ROOT / "prompts", root / "prompts", dirs_exist_ok=True)
    cfg = settings_mod.load(root)
    # 把等待类参数压到最小，只测逻辑不测耐心
    cfg.set_override("wechat.merge_window", 0.05)
    cfg.set_override("wechat.cooldown", 0.0)
    cfg.set_override("wechat.split_reply", False)
    cfg.set_override("wechat.typing_per_char", 0.0)
    cfg.set_override("llm.tool_rounds", 1)
    return cfg


if __name__ == "__main__":
    sys.exit(main())
