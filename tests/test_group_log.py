# -*- coding: utf-8 -*-
"""回归：关键用户的群消息记录。

    .venv\\Scripts\\python.exe tests\\test_group_log.py

验五组东西，**每条都是断言**：

  1. 谁算关键用户（三条判定链：显式记录 > 配置种子 > 默认等级）
  2. 记了哪些字段、存成什么样（一天一个 jsonl、一行一条）
  3. 重复投递只记一次、超长截断、坏行不影响其他行
  4. 读：时间正序、按群/按人过滤、limit、坏行跳过
  5. 接线的位置对不对（off 不记 / silent 记 / @ 判定之前就记 / 私聊不记）

隔离：全部写在系统临时目录，跑完删掉，**不碰 data/ 里的真实等级和记录**。
不联网、不调模型、不起微信。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.chat_locks import ChatLocks
from app.sender import Sender
from app.chat_policies import ChatPrefs
from app.panel.state import MonitorState
from app.group_log import MSG_FIELDS, TEXT_CAP, GroupLog
from app.user_levels import LEVELS, UserLevelStore
from _support import FAILS, check, line

GROUP = "12345678@chatroom"
PRIVATE = "wxid_friend1"



class FakeCfg:
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


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="deepchat-test-群记录-"))
    try:
        return _run(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(work: Path) -> int:
    print("=" * 52)
    print(" 关键用户群消息记录回归（临时目录）")
    print("=" * 52)
    print(f"  数据目录：{work}")

    _part_people(work / "people")
    _part_grouplog(work / "glog")
    _part_read(work / "read")
    _part_prune(work / "prune")
    _part_wiring(work / "wiring")

    line("")
    if FAILS:
        print(f" 共 {len(FAILS)} 项失败：")
        for name in FAILS:
            print("   - " + name)
        return 1
    print(" 全部通过")
    return 0


# ---------------------------------------------------------------- 1. 判定链
def _part_people(root: Path) -> None:
    line("1. 谁算关键用户（判定链）")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "people.json"

    store = UserLevelStore(path, owners=["wxid_boss"], key_users=["老张", "wxid_pal"],
                        default="stranger")
    check("没设过的人是默认等级", store.level_of("wxid_nobody") == "stranger")
    check("种子按 wxid 命中 -> 主人", store.level_of("wxid_boss") == "owner")
    check("种子按昵称命中 -> 熟人", store.level_of("wxid_x", "老张") == "friend")
    check("种子按 wxid 命中 -> 熟人", store.level_of("wxid_pal") == "friend")
    check("is_key 只认 owner/friend", store.is_key("wxid_boss") and
          not store.is_key("wxid_nobody"))

    store.set("wxid_pal", level="owner", alias="老板")
    check("显式记录盖过种子（friend -> owner）", store.level_of("wxid_pal") == "owner")
    check("别名一起存下来", store.alias_of("wxid_pal") == "老板")

    store.set("wxid_bad", level="blocked")
    check("blocked 不算关键用户", not store.is_key("wxid_bad"))

    reloaded = UserLevelStore(path, owners=["wxid_boss"], key_users=["老张"])
    check("重开实例能读回显式记录", reloaded.level_of("wxid_pal") == "owner")
    check("重开后种子仍然生效", reloaded.level_of("wxid_boss") == "owner")

    reloaded.apply_seeds(owners=["wxid_new"], key_users=[], default="friend")
    check("改种子不动显式记录", reloaded.level_of("wxid_pal") == "owner")
    check("改默认等级后，没记录的人跟着变", reloaded.level_of("wxid_nobody") == "friend")
    check("换掉的种子不再命中", reloaded.level_of("wxid_boss") == "friend")

    check("forget 之后回落默认", reloaded.forget("wxid_pal") and
          reloaded.level_of("wxid_pal") == "friend")

    try:
        reloaded.set("wxid_x", level="king")
        check("非法等级要抛 ValueError", False)
    except ValueError:
        check("非法等级要抛 ValueError", True)

    try:
        reloaded.set("", level="owner")
        check("空 wxid 要抛 ValueError", False)
    except ValueError:
        check("空 wxid 要抛 ValueError", True)

    broken = root / "broken.json"
    broken.write_text("这不是 json", encoding="utf-8")
    fallback = UserLevelStore(broken, default="stranger")
    check("people.json 坏了按空库启动，不抛异常",
          fallback.count() == 0 and fallback.level_of("wxid_x") == "stranger",
          fallback.load_error[:30])
    check("四个等级都在枚举里", len(LEVELS) == 4)


# ---------------------------------------------------------------- 2. 写入
def _part_grouplog(root: Path) -> None:
    line("2. 记了哪些字段、存成什么样")
    log = GroupLog(root, days=30)

    ok = log.append_msg(
        chat_id=GROUP, chat_name="测试群", sender="wxid_boss", sender_name="老板",
        level="owner", alias="老板", text="今天下午三点开会，别忘了带电脑",
        msg_key="k1", msg_type="text", msg_time=int(time.time()),
    )
    check("写一条返回 True", ok is True)

    items = log.read(limit=10)
    check("读回来 1 条", len(items) == 1, str(len(items)))
    record = items[0] if items else {}
    check("字段齐全（与 MSG_FIELDS 一致）", set(record) == set(MSG_FIELDS),
          str(sorted(set(MSG_FIELDS) - set(record))))
    check("op 是 msg", record.get("op") == "msg")
    check("群 id 原样保留", record.get("chat_id") == GROUP)
    check("等级一起记下来", record.get("level") == "owner")
    check("分数未接线时为 null（不是 0）",
          record.get("score") is None and record.get("reasons") is None)

    check("同一指纹重复写返回 False", log.append_msg(
        chat_id=GROUP, chat_name="测试群", sender="wxid_boss", sender_name="老板",
        level="owner", alias="", text="今天下午三点开会，别忘了带电脑",
        msg_key="k1",
    ) is False)
    check("重复的没多出一行", len(log.read(limit=10)) == 1)

    log.append_msg(
        chat_id=GROUP, chat_name="测试群", sender="wxid_pal", sender_name="老张",
        level="friend", alias="", text="收到", msg_key="k2",
    )
    check("换个指纹能写进去", len(log.read(limit=10)) == 2)

    long_text = "很长的一段话" * 200
    log.append_msg(
        chat_id=GROUP, chat_name="测试群", sender="wxid_boss", sender_name="老板",
        level="owner", alias="", text=long_text, msg_key="k3",
    )
    capped = [x for x in log.read(limit=10) if x.get("msg_key") == "k3"]
    check("超长正文被截断", bool(capped) and len(capped[0]["text"]) <= TEXT_CAP + 1,
          str(len(capped[0]["text"]) if capped else -1))
    check("截断有省略号标记",
          bool(capped) and capped[0]["text"].endswith("…"))

    log.append_reply(chat_id=GROUP, sender="wxid_boss", text="好，记下了",
                     ok=True, msg_keys=["k1"], level="owner")
    replies = [x for x in log.read(limit=10) if x.get("op") == "reply"]
    check("回复单独成行", len(replies) == 1)
    check("回复带上关联的 msg_keys",
          replies[0].get("msg_keys") == ["k1"] if replies else False)
    check("回复带上发送结果", replies[0].get("ok") is True if replies else False)

    path = log.path_for(time.time())
    check("文件名就是日期", path.name == time.strftime("%Y-%m-%d") + ".jsonl", path.name)
    check("一行一个 json（可逐行解析）",
          all(json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln))

    off = GroupLog(root / "off", enabled=False)
    check("关掉之后不写", off.append_msg(
        chat_id=GROUP, chat_name="", sender="w", sender_name="",
        level="owner", alias="", text="不该出现", msg_key="k9") is False)


# ---------------------------------------------------------------- 3. 读
def _part_read(root: Path) -> None:
    line("3. 读：顺序 / 过滤 / limit / 坏行")
    root.mkdir(parents=True, exist_ok=True)
    log = GroupLog(root, days=30)
    base = time.time() - 30

    for index in range(5):
        log.append_msg(
            chat_id=GROUP if index % 2 == 0 else "999@chatroom",
            chat_name="群", sender="wxid_boss" if index < 3 else "wxid_pal",
            sender_name="", level="owner", alias="",
            text=f"第{index}句", msg_key=f"r{index}", now=base + index,
        )

    items = log.read(limit=10)
    stamps = [x["ts"] for x in items if x.get("op") == "msg"]
    check("时间正序返回", stamps == sorted(stamps), str(stamps))
    check("条数对得上", len(stamps) == 5, str(len(stamps)))

    only_group = log.read(chat_id=GROUP, limit=10)
    check("按群过滤", len(only_group) == 3, str(len(only_group)))
    only_pal = log.read(sender="wxid_pal", limit=10)
    check("按人过滤", len(only_pal) == 2, str(len(only_pal)))
    check("limit 生效", len(log.read(limit=2)) == 2)

    with log.path_for(time.time()).open("a", encoding="utf-8") as fp:
        fp.write("{坏掉的一行\n")
    after = log.read(limit=10)
    check("坏行跳过、其余照读", len(after) == 5, str(len(after)))

    stats = log.stats()
    check("stats 能报今天写了多少行", stats["today"] >= 5, str(stats["today"]))
    check("stats 带 enabled 与 error", "enabled" in stats and "error" in stats)


# ---------------------------------------------------------------- 4. 清理
def _part_prune(root: Path) -> None:
    line("4. 保留期清理")
    root.mkdir(parents=True, exist_ok=True)
    old = root / "2020-01-01.jsonl"
    keep = root / (time.strftime("%Y-%m-%d") + ".jsonl")
    old.write_text("", encoding="utf-8")
    keep.write_text("", encoding="utf-8")
    stranger = root / "随手放的文件.jsonl"
    stranger.write_text("", encoding="utf-8")

    log = GroupLog(root, days=30)
    removed = log.prune()
    check("过期日文件被删", not old.exists() and removed >= 1, str(removed))
    check("当天的留着", keep.exists())
    check("不是日期命名的文件不碰", stranger.exists())


# ---------------------------------------------------------------- 5. 接线
def _part_wiring(root: Path) -> None:
    line("5. 接线：什么时候记、什么时候不记")
    root.mkdir(parents=True, exist_ok=True)
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)

    cfg = FakeCfg({
        "wechat": {"max_age_minutes": 5, "merge_window": 1.2,
                   "group_keywords": ["deep"], "group_at_only": True,
                   "reply_private": True},
        "people": {"owners": ["wxid_boss"], "default_level": "stranger"},
        "group": {"key_users": ["wxid_pal"], "log_enabled": True, "log_days": 30},
    })
    people = UserLevelStore(data / "people.json",
                         owners=cfg.get("people.owners"),
                         key_users=cfg.get("group.key_users"),
                         default="stranger")
    people.set("wxid_evil", level="blocked")
    glog = GroupLog(data / "group_log", days=30)
    chats = ChatPrefs(data / "chats.json")

    bot = _make_bot(cfg, people, glog, chats)
    fake_threading = _FakeThreading()
    import app.bot as bot_mod
    real_threading = bot_mod.threading
    bot_mod.threading = fake_threading
    try:
        _wiring_cases(bot, glog, chats, people)
    finally:
        bot_mod.threading = real_threading


def _wiring_cases(bot, glog, chats, people) -> None:
    def send(sender: str, text: str, chat: str = GROUP, name: str = "",
             local_id: int = 1, msg_type: str = "text") -> int:
        before = len(glog.read(limit=200))
        bot.on_message({
            "chat_id": chat, "chat_name": "测试群", "sender": sender,
            "sender_name": name, "is_self": False, "type": msg_type,
            "content": text, "time": int(time.time()),
            "raw": {"local_id": local_id},
        })
        return len(glog.read(limit=200)) - before

    check("主人发的话会被记下来", send("wxid_boss", "明天上午十点评审", name="老板") == 1)
    check("熟人发的话也会记", send("wxid_pal", "我下午过来", name="老张", local_id=2) == 1)
    check("陌生人不记", send("wxid_nobody", "今天有人吗", local_id=3) == 0)
    check("blocked 不记", send("wxid_evil", "在吗", local_id=4) == 0)
    check("私聊不记（私聊走记忆库）",
          send("wxid_boss", "晚上吃什么", chat=PRIVATE, local_id=5) == 0)
    check("太短的不记（「嗯」）", send("wxid_boss", "嗯", local_id=6) == 0)
    check("纯表情不记", send("wxid_boss", "👍👍👍", local_id=7) == 0)
    check("撤回提示不记",
          send("wxid_boss", "老板撤回了一条消息", local_id=8) == 0)
    # 占位（图片/表情）不是「他说了什么」，不该进流水。
    # 注意别拿 voice 当反例：微信开了「语音自动转文字」时，voice 的正文就是
    # 真实说的话，那种要记——见下面那条。
    check("占位（图片）不记",
          send("wxid_boss", "[图片·看不到内容]", msg_type="image", local_id=9) == 0)
    check("语音有转写就照记（那是真说的话）",
          send("wxid_boss", "晚上七点我在楼下等你", msg_type="voice", local_id=13) == 1)

    # 关键：判定为「不回」也要留痕（记录在 @ 判定之前）
    before_pending = bot._pending_count(GROUP)
    added = send("wxid_boss", "下午的会改到几点了", local_id=10)
    check("没 @ 被判为不回时仍然记录了", added == 1)
    check("被判定为不回就没进合并队列",
          bot._pending_count(GROUP) == before_pending)

    chats.set(GROUP, mode="silent")
    check("silent（只看不回）仍然记录", send("wxid_boss", "我先撤了", local_id=11) == 1)
    check("silent 不进合并队列",
          bot._pending_count(GROUP) == before_pending)
    chats.set(GROUP, mode="off")
    check("off 一句都不记", send("wxid_boss", "这条不该出现", local_id=12) == 0)
    chats.forget(GROUP)

    # 回复记录只对关键用户写
    reply_before = len([x for x in glog.read(limit=200) if x.get("op") == "reply"])
    bot._record_key_reply(GROUP, {"sender": "wxid_boss", "sender_name": "老板"},
                          "好，改到四点", True, ["k1"])
    bot._record_key_reply(GROUP, {"sender": "wxid_nobody", "sender_name": ""},
                          "不该出现", True, ["k2"])
    replies = [x for x in glog.read(limit=200) if x.get("op") == "reply"]
    check("回复只给关键用户记", len(replies) - reply_before == 1, str(len(replies)))
    check("回复内容与发送结果都在",
          replies[-1]["text"] == "好，改到四点" and replies[-1]["ok"] is True)


# ---------------------------------------------------------------- 桩
class _FakeTimer:
    """不真起线程：这个用例验的是「记不记」，不是「回不回」。"""

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.cancelled = False

    def start(self) -> None:
        return None

    def cancel(self) -> None:
        self.cancelled = True


class _FakeThreading:
    Timer = _FakeTimer
    Lock = threading.Lock
    RLock = threading.RLock


def _make_bot(cfg, people, glog, chats):
    """不走 Bot.__init__：那里要加载向量模型，这里只验记录接线。"""
    import app.bot as bot_mod

    bot = bot_mod.Bot.__new__(bot_mod.Bot)
    bot.cfg = cfg
    bot.people = people
    bot.glog = glog
    bot.chats = chats
    bot.state = MonitorState()
    bot.bridge = None
    bot.me = "deepChat"
    bot._turns = {}
    bot._pending = {}
    bot._timers = {}
    bot.chat_locks = ChatLocks()
    bot.sender = Sender(bot.cfg, lambda: None)   # 没有接入层：语音/发送都直接返回
    bot._last_sent = {}
    bot._lock = threading.Lock()
    bot._seen = bot_mod._Seen(Path(glog.dir) / "seen.json")
    return bot


if __name__ == "__main__":
    sys.exit(main())
