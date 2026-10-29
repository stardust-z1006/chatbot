# -*- coding: utf-8 -*-
"""编排：一条消息从进来到出去。

顺序是固定的：检索 -> 存记忆 -> 闸门拼上下文 -> 调模型 -> 切分 -> 发出去。
检索必须在写入之前，否则这条消息会把自己捞出来（相似度 1.00）。
每一步失败都降级成一句人话，绝不让对方看到一个报错。

同一会话串行（否则上下文会错乱），不同会话并行。自己发的消息、处理过的消息都丢掉。
群里只有关键用户的话进记忆库，且带群 id 做跨群隔离；上下文按人分（群聊渲染成「名字：话」）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .embeddings import Embedder
from .group_engage import GroupEngager, MemberCache, TalkState, strip_mentions
from .context_budget import ContextBudget
from .group_log import GroupLog
from .dedup_judge import DedupJudge
from .kb_store import KbStore
from .chat_policies import ChatPrefs, is_group
from .llm import LLMError, build_llm, estimate_tokens
from .memory import MemoryStore
from .user_levels import UserLevelStore, is_key_level
from .personas import PersonaStore
from .retrieval import MemoryRetriever
from .message_tagging import NOISE_PATTERNS, classify, clip_for_store, worth_remembering
from .panel.state import MonitorState
from .tools import TurnContext
from .tools import build as build_tools
from .tools.history import HistoryReader
from .voice.tts import TTS
from .wechat.reply_split import clean_for_wechat, split_reply

logger = logging.getLogger(__name__)

TEXT_TYPES = {"text", "quote", "link", "file", "unknown"}
# 按等级裁轮数：主人上下文更长（更熟），陌生人只留最近几轮。
# 值是「轮数」，_trim_turns 里乘 2 才是条数（一问一答两条）。
LEVEL_TURNS = {"owner": 16, "friend": 12, "stranger": 8}
SEEN_CAP = 5000
# 群记录的最短长度。比记忆入库的 MIN_CHARS(5) 松：记录是流水，不是事实，
# 「嗯」这种两字以内仍然不记（没有信息量），「好嘞」记下来也无害。
MIN_RECORD_CHARS = 2
# 轮次用尽仍想调工具时，用这句逼它说人话（配合 tools=None 使用）
FORCE_FINISH = "（工具额度用完了，别再调工具，直接用上面拿到的信息回答对方。）"


class Bot:
    def __init__(self, cfg: Any, bridge: Any = None,
                 state: Optional[MonitorState] = None) -> None:
        self.cfg = cfg
        self.bridge = bridge
        # 监控状态：不传就自己建一个（命令行 ask 时也有数字，虽然没人看）
        self.state = state or MonitorState()
        # 多角色 + 按会话策略。两者都是「改了立刻生效」，所以每轮现读。
        self.personas = PersonaStore(
            cfg.rel(cfg.get("persona.dir", "prompts/personas")),
            legacy_file=cfg.rel(cfg.get("persona.prompt_file", "prompts/persona.md")),
            state_path=Path(cfg.data_dir) / "personas.json",
        )
        self.chats = ChatPrefs(Path(cfg.data_dir) / "chats.json")
        self.persona = self.personas.text_for("")

        # 用户等级 + 关键用户的群消息记录。两个都是「建不起来只少点功能」，
        # 所以跟 kb / reader / tts 一样：失败降级，不拦启动。
        self.people: Any = None
        self.glog: Any = None
        try:
            self.people = UserLevelStore(
                Path(cfg.data_dir) / "people.json",
                owners=list(cfg.get("people.owners") or []),
                key_users=list(cfg.get("group.key_users") or []),
                default=str(cfg.get("people.default_level", "stranger")),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("用户等级读不到，所有人按陌生人处理：%s", exc)
        try:
            self.glog = GroupLog(
                Path(cfg.data_dir) / "group_log",
                days=int(cfg.get("group.log_days", 30)),
                enabled=bool(cfg.get("group.log_enabled", True)),
            )
            self.glog.prune()
        except Exception as exc:  # noqa: BLE001
            logger.warning("群消息记录不可用，这次不记：%s", exc)

        # 启动时各留一行，运维一眼就能看出「等级读到几条」「记录在哪、留几天」。
        # 只在这里打：UserLevelStore / GroupLog 在 web 模式下每次请求都会新建，
        # 在那两个类里打日志会变成每 2 秒刷一条。
        if self.people is not None:
            self.people.ensure_file()
            logger.info("等级库就绪：%d 条（默认 %s）",
                        self.people.count(), self.people.describe()["default"])
        if self.glog is not None:
            logger.info("群记录就绪：%s（保留 %d 天，%s）",
                        self.glog.dir, self.glog.days,
                        "开启" if self.glog.enabled else "关闭")

        # 接话判定需要的两个内存状态。要建在 apply_config() 之前：
        # apply_config() 里会重建 GroupEngager，它引用这两个对象。
        # 连续性窗口默认 90s、连说满 3 轮延到 180s；成员表缓存 600s。
        self.talk = TalkState(
            window=float(cfg.get("group.engage.continuity_window", 90)),
            extend=180.0,
        )
        self.members = MemberCache(
            self._group_members,
            ttl=float(cfg.get("group.engage.member_ttl", 600)),
        )

        self.embedder = Embedder()
        self.store = MemoryStore(
            Path(cfg.data_dir) / "memory",
            embedder=self.embedder,
            capacity=int(cfg.get("memory.capacity", 400)),
            group_quota=int(cfg.get("memory.group_quota", 80)),
            judge=DedupJudge(cfg),
        )
        # gate / retriever / llm / tts 全部交给 apply_config() 建，这里不重复写：
        # 面板改参数走的是同一个入口，保证「刚启动」和「热改之后」的状态不会走样。
        self.budget: Any = None
        self.retriever: Any = None
        self.llm: Any = None
        self.tts: Optional[TTS] = None
        self.apply_config()

        # 三样「可有可无」的能力。设计前提是：任何一项初始化失败都只是少一个工具，
        # 聊天本身照常——联网挂了还能聊天，是这套东西能不能长期跑的关键。
        self.kb = self._init_kb(cfg)
        self.reader = self._init_reader(cfg, bridge)

        # 上下文按会话存，每条 user 消息还带着「谁说的」（name / uid）。
        # 只存文字的话，群里三个人的话在模型眼里就是一个人在自言自语。
        self._turns: Dict[str, List[Dict[str, Any]]] = {}
        # 合并队列按「会话 + 说话人」排队，见 _pending_key。
        # 值是 {"msg": 这条消息, "items": [(指纹, 正文)]}：msg 要跟着一起排队，
        # 否则拆分之后每个说话人该用谁的名字回复就无从得知。
        self._pending: Dict[tuple, Dict[str, Any]] = {}
        self._timers: Dict[tuple, threading.Timer] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._last_sent: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.me = ""
        self._seen = _Seen(Path(cfg.data_dir) / "seen.json")

    # ================================================================ 配置热应用
    def apply_config(self) -> None:
        """按当前 cfg 重建所有「跟着参数走」的对象。

        面板改完参数调这一下，下一条消息就按新值跑，不用重启。
        重建的都是轻量对象（ContextBudget / MemoryRetriever / LLM / TTS），代价可以忽略；
        真正贵的 Embedder 和 MemoryStore 不在这里动。

        改不动的只有两类，schema 里标了 restart：
          - 监听轮询间隔（Listener 线程已经按旧值跑起来了）
          - 资料库扫描目录（要重建索引才有内容）
        """
        cfg = self.cfg
        self.budget = ContextBudget(
            context_limit=int(cfg.get("llm.context_limit", 32000)),
            reply_reserve=int(cfg.get("llm.max_tokens", 1200)),
        )
        self.retriever = MemoryRetriever(
            self.store,
            embedder=self.embedder,
            weight_similarity=float(cfg.get("memory.weight_similarity", 0.7)),
            weight_importance=float(cfg.get("memory.weight_importance", 0.3)),
            top_k=int(cfg.get("memory.top_k", 6)),
            min_score=float(cfg.get("memory.min_score", 0.55)),
        )
        self.llm = build_llm(cfg)
        # 容量调小了要立刻收一遍，不然得等下一条记忆写入才见效
        self.store.capacity = max(10, int(cfg.get("memory.capacity", 400)))
        self.store.group_quota = max(0, int(cfg.get("memory.group_quota", 80)))
        self.store.enforce_capacity()
        self.tts = self._init_tts(cfg)
        _apply_log_level(str(cfg.get("log.level", "INFO")))

        # 等级种子和群记录参数是「面板改了要立刻生效」的那一类。
        # 对象本身不重建（UserLevelStore 里可能已有显式记录，重建会丢），只同步字段。
        if getattr(self, "people", None) is not None:
            self.people.apply_seeds(
                owners=list(cfg.get("people.owners") or []),
                key_users=list(cfg.get("group.key_users") or []),
                default=str(cfg.get("people.default_level", "stranger")),
            )
        if getattr(self, "glog", None) is not None:
            self.glog.enabled = bool(cfg.get("group.log_enabled", True))
            self.glog.days = max(1, int(cfg.get("group.log_days", 30)))

        # 接话打分的阈值（call_score / join_owner / join_friend）是热改项：
        # 面板改完要立刻按新值判，所以每次 apply_config 都重建 GroupEngager。
        # S4 才在 config.yaml 加 group.engage 段，现在拿不到就落到默认值，先能跑。
        self.engager = GroupEngager(cfg, getattr(self, "people", None), self.talk, self.members)
        self.engager.state = self.state

    def reload_persona(self) -> str:
        """重新读默认人设。面板改完角色文件调这个。"""
        self.persona = self.personas.text_for("")
        return self.persona

    def persona_for(self, chat_id: str) -> str:
        """这个会话该用哪个人设：会话策略里绑的 > 默认角色。

        绑定的「事实来源」只有一处——`data/chats.json`（ChatPrefs）。
        人设库只负责「有哪些角色、内容是什么」，不另存一份绑定，
        否则改了一边另一边不知道，排查起来要命。
        """
        name = self.chats.persona_for(chat_id)
        if name:
            text = self.personas.text(name)
            if text:
                return text
        return self.personas.text_for("")

    def _who(self, sender: str, sender_name: str = "") -> str:
        """显示名：人工称呼(alias) > 微信显示名 > wxid 尾号。

        `_identity_block` 和上下文里的「说话人前缀」必须用同一个名字，
        否则会出现「system 里叫他老张、对话里写着张伟」这种自相矛盾的上下文。
        """
        people = getattr(self, "people", None)
        alias = people.alias_of(sender) if (people is not None and sender) else ""
        if alias:
            return str(alias).strip()
        name = str(sender_name or "").strip()
        if name:
            return name
        return sender[-6:] if sender else ""

    def _identity_block(self, sender: str, sender_name: str, is_group: bool) -> str:
        """告诉模型「现在跟谁说话」，并把称呼定死。

        两个理由，缺了就会重演 2026-10-27 那次「所有人都叫老大」：

        1. **称呼只来自 `people.json`（人工设定），不来自记忆。** 记忆是模型自己写的，
           一次误记就对所有人生效；人工设定的还能在面板上改回来。
        2. **上下文里只有文字，没有「谁说的」。** 不写这句，模型只能靠猜；
           群聊里尤其明显 —— 三个人说话，它看起来像一个人在自言自语。
        """
        if not sender:
            return ""
        people = getattr(self, "people", None)
        level = people.level_of(sender, sender_name) if people is not None else "stranger"
        who = self._who(sender, sender_name) or sender[-6:]
        if level == "owner":
            return f"\n\n【对方】正在跟你说话的是你的主人，你叫他「{who}」。可以更主动、更亲近。"
        if level == "friend":
            return f"\n\n【对方】这是你认识的人，你叫他「{who}」。"
        return f"\n\n【对方】正在跟你说话的是「{who}」，你还不熟，别乱认亲。"

    # ================================================================ 可选依赖
    @staticmethod
    def _init_kb(cfg: Any) -> Optional[KbStore]:
        """资料库索引。单独一份 SQLite，坏了重建就行，所以失败只降级不中断。"""
        if not cfg.get("kb.enabled", True):
            return None
        try:
            return KbStore(Path(cfg.data_dir) / "kb.sqlite3")
        except Exception as exc:  # noqa: BLE001
            logger.warning("资料库打不开，kb_search 这轮不挂：%s", exc)
            return None

    @staticmethod
    def _init_reader(cfg: Any, bridge: Any) -> Optional[HistoryReader]:
        """翻聊天记录。要有接入层才能读库——命令行 ask 模式没有 bridge，直接跳过。"""
        if bridge is None or not cfg.get("history.enabled", True):
            return None
        try:
            return HistoryReader(bridge.db, bridge.self_wxid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("聊天记录读不到，chat_history 不挂：%s", exc)
            return None

    @staticmethod
    def _init_tts(cfg: Any) -> Optional[TTS]:
        """语音合成。只在开关打开时建，默认关着——edge-tts 要联网，没必要白担风险。"""
        if not cfg.get("voice.enabled", False):
            return None
        try:
            return TTS(
                out_dir=Path(cfg.data_dir) / "voice",
                voice=str(cfg.get("voice.voice", "zh-CN-XiaoxiaoNeural")),
                rate=str(cfg.get("voice.rate", "+8%")),
                pitch=str(cfg.get("voice.pitch", "+0Hz")),
                max_chars=int(cfg.get("voice.tts_max_chars", 220)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("语音合成不可用，本轮只发文字：%s", exc)
            return None

    # ================================================================ 生命周期
    def start(self) -> None:
        if self.bridge is None:
            raise RuntimeError("没有接入层，不能监听")
        self.me = self.bridge.self_info().get("nickname", "")
        # 提前把界面通道建好：一是第一条消息能立刻发出去，
        # 二是面板从一开始就能看到「微信窗口是不是被最小化了」（最小化会发不出）
        try:
            self._bridge_window()
        except Exception as exc:  # noqa: BLE001
            logger.warning("微信界面通道没建起来（发消息时会再试一次）：%s", exc)
        self.state.running = True
        self.state.account = self.me
        self.bridge.listen(
            list(self.cfg.get("wechat.listen") or []),
            self.on_message,
            interval=float(self.cfg.get("wechat.poll_interval", 1.0)),
        )
        logger.info("机器人已启动（账号 %s），窗口别关", self.me)

    def _bridge_window(self) -> None:
        """让接入层把微信界面通道建起来（幂等，失败不影响收消息）。"""
        if self.bridge is None:
            return
        getattr(self.bridge, "ensure_ready", lambda: None)()

    def stop(self) -> None:
        self.state.running = False
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
        if self.bridge is not None:
            self.bridge.stop()
        if getattr(self, "glog", None) is not None:
            self.glog.close()
        self.store.snapshot()

    # ================================================================ 收
    def on_message(self, msg: Dict[str, Any]) -> None:
        if msg.get("is_self"):
            return
        if (msg.get("type") or "") not in TEXT_TYPES:
            logger.debug("忽略非文本消息（%s）", msg.get("type"))
            return
        text = str(msg.get("content") or "").strip()
        if not text:
            return

        # 只回新鲜消息。首次启动时监听器可能把没处理过的旧消息翻出来，
        # 隔了一夜还回一句「在啊」很怪（实测踩过：回了昨晚 23:21 的消息）。
        max_age = float(self.cfg.get("wechat.max_age_minutes", 5)) * 60
        stamp = float(msg.get("time") or 0)
        if stamp and max_age > 0 and time.time() - stamp > max_age:
            logger.info("跳过过期消息（%.0f 分钟前）：%s",
                        (time.time() - stamp) / 60, text[:16])
            return
        chat_id = msg.get("chat_id") or ""
        if not chat_id:
            return

        # 会话策略：每个群/每个人可以单独设「正常回 / 只在@时回 / 只看不回 / 完全不看」。
        # 判在去重和合并之前——不看就是不看，别在 seen 里留指纹（不然以后改了策略也补不回来）。
        mode = self.chats.mode_for(chat_id, self.cfg)
        if mode == "off":
            return
        # 指纹用「原始正文」算一次，之后三处共用：记录 / 去重 / 回复关联。
        # 分开算会出事：记录要原话（带 @），去重要剥掉 @ 的正文，两边的 text 不同，
        # 碰上没有 local_id 的消息就退化成「群|时间|正文」，于是回复记录关联不到那条 msg。
        key = _msg_key(msg, text)
        # 关键用户的群消息留一份底。位置有讲究，三处都不能挪：
        #   1. 在 off 之后：off 是「完全不看」，连日志都不写，更不该留正文
        #   2. 在 silent 之前：silent 是「只看不回」，恰恰是最需要留痕的场景
        #   3. 在 @ 判定之前：被判定为不回的消息同样要留痕，否则没法算准召
        self._record_key_message(chat_id, msg, text, key)
        if mode == "silent":
            logger.debug("只看不回（%s）：%s", chat_id, text[:24])
            return
        # 群里还可以只理某些人 / 不理某些人（名单为空 = 所有人都算）
        if not self.chats.allow_sender(
            chat_id, str(msg.get("sender") or ""), str(msg.get("sender_name") or "")
        ):
            logger.debug("按成员规则跳过：%s @ %s", msg.get("sender_name"), chat_id)
            return
        passed, _verdict = self._judge(chat_id, msg, text, mode)
        if not passed:
            return
        text = strip_mentions(text, self._self_names(chat_id))

        # 指纹沿用上面算好的那个（不能用剥完 @ 的 text 重算，见上）
        if not self._seen.add(key):
            logger.info("重复消息，跳过：%s", text[:20])
            return

        with self._lock:
            # 指纹跟着文本一起排队：处理失败时要按指纹撤销，否则这条消息
            # 在 seen 里已经算「处理过」，重启或重投都不会再补。
            pkey = self._pending_key(chat_id, msg)
            slot = self._pending.get(pkey)
            if slot is None:
                slot = {"msg": msg, "items": []}
                self._pending[pkey] = slot
            slot["items"].append((key, text))
            timer = self._timers.pop(pkey, None)
            if timer is not None:
                timer.cancel()
            delay = float(self.cfg.get("wechat.merge_window", 1.2))
            timer = threading.Timer(delay, self._flush, args=(pkey,))
            timer.daemon = True
            self._timers[pkey] = timer
            timer.start()

    @staticmethod
    def _pending_key(chat_id: str, msg: Dict[str, Any]) -> tuple:
        """合并队列的键：(会话, 说话人)。

        必须按人拆开。按会话排队的话，群里 A、B 在 1.2 秒内各说一句会被拼成
        **一条** query —— 上下文分人在这之前就已经错了一半：模型收到的是
        「A的话\\nB的话」这样一段没有主语的文本，只能猜。

        私聊里 sender 只有一个值，所以键退化成「按会话」，行为和以前一致。
        群聊拿不到 sender 时也退回空串（按会话合并）：宁可合错，别把话丢了。
        """
        sender = str(msg.get("sender") or "") if is_group(chat_id) else ""
        return (chat_id, sender)

    def _pending_count(self, chat_id: str) -> int:
        """这个会话还攒着几条没处理的消息。合并队列拆按人之后，
        要看「进没进队列」只能按会话汇总，不能再拿 chat_id 直接取。"""
        with self._lock:
            return sum(len(slot["items"]) for (cid, _), slot in self._pending.items()
                       if cid == chat_id)

    def _chat_lock(self, chat_id: str) -> threading.Lock:
        """拿这个会话专属的锁。同会话串行、不同会话并行。

        合并窗口只挡得住「连着发来好几条」，挡不住「上一条还在跑工具循环，
        这一条已经到点了」：那种情况下会同时有两个 Timer 线程跑同一个会话，
        把 `_turns` 交错、把回复顺序打乱、甚至对同一条消息答两次。
        所以锁必须覆盖 handle + 发送的整个周期。

        锁对象按会话缓存，超过 512 个只清「当前没被持有」的，避免长期跑
        一个群一个锁地涨上去。
        """
        with self._lock:
            lock = self._locks.get(chat_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[chat_id] = lock
                if len(self._locks) > 512:
                    for key in [k for k, v in self._locks.items()
                                if k != chat_id and not v.locked()]:
                        self._locks.pop(key, None)
            return lock

    def _flush(self, pkey: tuple) -> None:
        chat_id = pkey[0]
        with self._lock:
            slot = self._pending.pop(pkey, None)
            self._timers.pop(pkey, None)
        if not slot or not slot["items"]:
            return
        items = slot["items"]
        msg = slot["msg"]
        keys = [key for key, _ in items]
        name = msg.get("chat_name") or chat_id
        query = "\n".join(text for _, text in items)
        # 锁仍按会话：串行边界是「这个群一条一条回」，不是「这个人一条一条回」。
        # 按人加锁的话，A、B 同时到点会并发跑同一群的 handle，回复顺序就乱了。
        with self._chat_lock(chat_id):
            self._flush_body(chat_id, name, query, keys, msg)

    def _flush_body(self, chat_id: str, name: str, query: str,
                    keys: List[str], msg: Dict[str, Any]) -> None:
        """真正干活的那段，调用方负责持会话锁。"""
        try:
            result = self.handle(
                chat_id, query, chat_name=name,
                sender=str(msg.get("sender") or ""),
                sender_name=str(msg.get("sender_name") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("处理消息失败：%s", exc, exc_info=True)
            self.state.error()
            # 撤销指纹，否则这条消息永远算「处理过」：一句话就这么没了
            for key in keys:
                self._seen.discard(key)
            return
        logger.info("回复（%.1fs）：%s", result["elapsed"], result["reply"][:60])
        sent = self._send(chat_id, result["reply"])
        # 发送成功才开连续性窗口：发送失败还开窗，会让「我以为在跟你聊，其实你没收到」。
        if sent and is_group(chat_id):
            self.talk.open(chat_id, str(msg.get("sender") or ""))
        # 文字先落地，再补语音。反过来万一语音卡住，对方会先看到一段空白
        self._send_voice(chat_id, result.get("voice") or [])
        # 回复也留一行，和 msg 行靠 msg_keys 关联（不回写 msg 行，见 grouplog）
        self._record_key_reply(chat_id, msg, result["reply"], sent, keys)
        # 埋点放在最后：面板要看到的是「这一条真的走完了」的耗时
        self.state.reply_sent(sent)
        smeta = result.get("stats") or {}
        self.state.tokens(smeta.get("tokens", 0), smeta.get("budget", 0))
        self.state.message(
            chat=chat_id,
            chat_name=name,
            text=query,
            reply=result["reply"],
            elapsed=result["elapsed"],
            tools=result.get("tools") or [],
            ok=sent or self.bridge is None,
        )

    # ================================================================ 群记录
    def _record_key_message(self, chat_id: str, msg: Dict[str, Any], text: str,
                            key: str = "") -> None:
        """关键用户在群里说的话，追加一条流水。**记录不等于放行**。

        识别的三级判定链在 `user_levels.py` 里（记录 > 配置种子 > 默认），这里只负责
        「三条同时满足才记」：

          1. 群聊（`@chatroom`）—— 私聊不记，私聊本来就进记忆库，再记一遍是冗余
          2. 等级是 owner / friend —— `blocked` 在 `level_of` 就返回了，天然不记
          3. 文本不是噪音：>= 2 字、含字母数字（纯表情不记）、不是撤回/入群提示

        为什么不借 `_Seen` 去重：`_Seen` 管的是「要不要回」，在 @ 判定**之后**才写；
        这里管的是「要不要留痕」，包括被判定为不回的那些。两件事互不顶替，
        所以 GroupLog 自己带一层按 msg_key 的去重。

        Args:
            key: 消息指纹。由调用方按**原始正文**算好传进来，保证「记录 / 去重 /
                回复关联」三处是同一个 key（细节见 on_message 里的注释）。
        """
        glog = getattr(self, "glog", None)
        people = getattr(self, "people", None)
        if glog is None or people is None or not is_group(chat_id):
            return
        sender = str(msg.get("sender") or "")
        if not sender:
            return
        name = str(msg.get("sender_name") or "")
        level = people.level_of(sender, name)
        if not is_key_level(level):
            logger.debug("群记录跳过（%s 是 %s）：%s", name or sender, level, text[:20])
            return
        probe = (text or "").strip()
        if len(probe) < MIN_RECORD_CHARS or not any(ch.isalnum() for ch in probe):
            return
        if any(pattern in probe for pattern in NOISE_PATTERNS):
            return
        glog.append_msg(
            chat_id=chat_id,
            chat_name=str(msg.get("chat_name") or chat_id),
            sender=sender,
            sender_name=name,
            level=level,
            alias=people.alias_of(sender),
            text=probe,
            msg_key=key or _msg_key(msg, probe),
            msg_type=str(msg.get("type") or "text"),
            msg_time=float(msg.get("time") or 0),
        )

    def _record_key_reply(self, chat_id: str, msg: Dict[str, Any], reply: str,
                          ok: bool, keys: List[str]) -> None:
        """回复单独记一行。发送失败也记（`ok=False`）——「回了但没送出去」正是要查的。"""
        glog = getattr(self, "glog", None)
        people = getattr(self, "people", None)
        if glog is None or people is None or not is_group(chat_id):
            return
        sender = str(msg.get("sender") or "")
        if not sender:
            return
        level = people.level_of(sender, str(msg.get("sender_name") or ""))
        if not is_key_level(level):
            return
        glog.append_reply(
            chat_id=chat_id, sender=sender, text=reply, ok=ok,
            msg_keys=keys, level=level,
        )

    # ================================================================ 想
    def handle(self, chat_id: str, user_text: str, chat_name: str = "",
               sender: str = "", sender_name: str = "") -> Dict[str, Any]:
        """跑完整链路，返回回复和诊断信息。发送不在这里做，方便命令行测。

        Args:
            sender: 说话人的 wxid。**记忆的归属键就是它**（`u:<wxid>`）。
                留空 = 命令行/自检，没有身份，检索不过滤。
            sender_name: 显示名，只用于告诉模型「跟谁说话」，不参与归属判定
                （人工称呼在 `people.alias`，见 `_who`）。
        """
        started = time.time()

        # 0. 指令先截住：它们不该进记忆、不该检索、更不该调模型
        command = self._command(chat_id, user_text)
        if command is not None:
            return {
                "reply": command,
                "elapsed": time.time() - started,
                "stats": {"tokens": 0, "budget": 0, "turns": 0, "memories": 0},
                "memories": [],
                "types": {},
                "voice": [],
                "tools": [],
            }

        # 记忆的归属（2026-10-27 加）。没有这一层，记忆库就是所有人共读共写的大锅：
        # A 说一句「叫我老大」，B 进来检索照样命中，于是对谁都喊老大。
        #
        # 两条边界仍然保留，只是从「一律不记」收紧成「按人记」：
        #   1. 隐私：私聊记忆 owner=u:自己、scope=private，群里被 @ 也捞不出来
        #   2. 污染：群里只有关键用户（owner/friend）才入库，且带 chat_id 做跨群隔离
        is_group = "@chatroom" in (chat_id or "")
        owner = f"u:{sender}" if sender else ""
        scope = "group" if is_group else "private"
        # 等级在这轮开头算一次，检索门槛、裁剪轮数、身份句三处共用，避免各算各的。
        people = getattr(self, "people", None)
        level = people.level_of(sender, sender_name) if people is not None else "stranger"
        if is_group:
            # 陌生人不在群里留痕（名额和隐私都不划算）；people 没起来时整条记忆链关闭
            mem_ok = bool(owner) and people is not None and is_key_level(level)
        else:
            mem_ok = bool(owner)
        group_chat = chat_id if is_group else ""

        # 1. 先检索。必须在写入之前——否则这条消息会把自己检索出来（相似度 1.00），
        #    白占一个记忆名额，看起来像「她记得你刚说的话」这种废话。
        #    sender 为空（命令行 / 自检）时不过滤，保持「本地调试能看到全部记忆」。
        if mem_ok:
            # 主人看得更多、门槛更低：这是「更高待遇」在检索这一侧的落点。
            kw = {"mark_used": True, "owner": owner, "scope": scope, "chat_id": group_chat}
            if level == "owner":
                kw.update(top_k=8, min_score=0.45)
            hits = self.retriever.search(user_text, **kw)
        elif not sender:
            hits = self.retriever.search(user_text, mark_used=True)
        else:
            hits = []

        # 2. 再记下来。写不进去也要继续回，记忆是增强不是前置条件
        if mem_ok and worth_remembering(user_text):
            try:
                self.store.put(clip_for_store(user_text), owner=owner,
                               scope=scope, chat_id=group_chat)
            except Exception as exc:  # noqa: BLE001
                logger.warning("写记忆失败，继续回复：%s", exc)
        elif mem_ok:
            logger.debug("不值得存，跳过：%s", user_text[:20])

        # 3. 拼上下文（唯一闸门）。人设按会话取：这个群绑了别的角色就用那个；
        #    身份说明按「谁在说话」取，这是模型分清 A 和 B 的唯一依据。
        #    上下文里每条 user 消息都记下是谁说的：群聊渲染时会变成「老板：……」，
        #    模型这才分得清「A 说他下周三出差」和「B 问去哪」。
        turns = self._turns.setdefault(chat_id, [])
        turns.append({
            "role": "user",
            "content": user_text,
            "name": self._who(sender, sender_name),
            "uid": sender,
        })
        assembled = self.budget.build(
            self.persona_for(chat_id) + self._identity_block(sender, sender_name, is_group),
            turns, hits,
            label_speakers=is_group,
        )

        # 4. 调模型 + 工具循环。工具每轮新建，闭包里带着本轮会话信息，多会话不会串。
        ctx = TurnContext(
            chat_id=chat_id,
            chat_name=chat_name or chat_id,
            is_group=is_group,
            owner=owner if mem_ok else "",
            scope=scope,
        )
        registry = build_tools(
            self.cfg, self.store, ctx, reader=self.reader, kb=self.kb
        )
        try:
            reply, meta = self._run_tools(
                assembled.messages,
                registry,
                # 闸门只管「拼装那一刻」，管不到工具循环里追加的结果。
                # 这里把总上限传进去：上下文 = 拼装 + 工具结果 + 留给回复的位置，
                # 三样加起来不能超模型窗口，否则接口直接报「上下文超长」。
                ceiling=int(self.cfg.get("llm.context_limit", 32000))
                - int(self.cfg.get("llm.max_tokens", 1200)),
            )
        except LLMError as exc:
            logger.error("模型调用失败：%s", exc)
            self.state.error()
            reply, meta = "我这会儿有点卡，等下再说？", {"tools": [], "rounds": 0}
        except Exception as exc:  # noqa: BLE001
            logger.error("模型调用异常：%s", exc, exc_info=True)
            self.state.error()
            reply, meta = "我这会儿有点卡，等下再说？", {"tools": [], "rounds": 0}

        # 5. 收尾
        reply = clean_for_wechat(reply)
        turns.append({"role": "assistant", "content": reply})
        self._trim_turns(turns, LEVEL_TURNS.get(level, 8) * 2)

        # 语音：tool 模式由模型自己用 speak 排；always 模式每条都配
        voice = list(ctx.pending_voice)
        if self.tts is not None and self.cfg.get("voice.mode", "tool") == "always":
            voice = [reply]

        return {
            "reply": reply,
            "elapsed": time.time() - started,
            "stats": assembled.stats,
            "memories": [h.brief() for h in hits],
            "types": classify(user_text).as_dict(),
            "voice": voice,
            "tools": meta.get("tools", []),
        }

    # ================================================================ 工具循环
    def _run_tools(
        self, messages: List[Dict[str, Any]], registry: Any, ceiling: int = 0
    ) -> tuple:
        """问模型 → 有 tool_calls 就执行并回填 → 再问，直到出文字。

        messages 是闸门刚拼出来的副本，往里追加不会污染 self._turns，
        所以工具往返只活在这一轮里，下一轮对话不会带着一堆工具结果。

        Args:
            ceiling: 这一轮**总共**能用到的 token 上限（已经扣掉留给回复的位置）。
                0 表示不额外限制（只按工具体预算走）。

        Returns:
            tuple[str, dict]: （回复正文, {"tools": [调用过的工具名], "rounds": 实际轮数}）

        Raises:
            LLMError: 模型调用失败（由 handle 统一降级成一句人话）。
        """
        rounds = max(1, int(self.cfg.get("llm.tool_rounds", 4)))
        budget = max(0, int(self.cfg.get("llm.tool_budget", 6000)))
        specs = registry.specs()
        spent = 0
        calls: List[str] = []
        text = ""
        finished = False

        for index in range(rounds):
            # 每一轮开头都量一次：工具结果是往里追加的，不量就会一路加爆窗口。
            # 注意别再额外加 spent——messages 里已经包含了先前追加的 role=tool
            # 内容，再加一遍等于把工具结果按两份算，会在还有空间时就提前收尾。
            if ceiling and estimate_messages(messages) > ceiling:
                logger.warning(
                    "上下文快满了（%d > %d），停用工具让模型直接回答",
                    estimate_messages(messages), ceiling,
                )
                messages.append(
                    {
                        "role": "user",
                        "content": "（上下文快满了，别再查了，用已经拿到的信息直接回答。）",
                    }
                )
                out = self.llm.chat(messages)
                if out.wants_tools or not (out.content or "").strip():
                    # 少数模型会无视「别再查了」再要一次工具：再劝一次就收，绝不多轮
                    messages.append(
                        {"role": "user", "content": "（直接回答，不要再调用工具。）"}
                    )
                    out = self.llm.chat(messages)
                text = (out.content or "").strip() or "（我这边上下文满了，你再说一次？）"
                finished = True
                break

            out = self.llm.chat(messages, tools=specs)
            if not out.wants_tools:
                text = out.content
                finished = True
                break

            # 必须原样回填（含 tool_calls），否则下一轮模型不知道自己刚才要求过什么
            messages.append(out.as_msg_dict())
            for call in out.tool_calls:
                started = time.perf_counter()
                result = registry.call(call["name"], call["arguments"])
                self.state.tool(
                    call["name"], (time.perf_counter() - started) * 1000, _tool_ok(result)
                )
                result, cost = _clip_result(result, budget - spent)
                spent += cost
                calls.append(call["name"])
                logger.info(
                    "工具 %s(%s) → %d 字（结果预算 %d/%d token）",
                    call["name"], call["arguments"][:60], len(result), spent, budget,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "content": result,
                    }
                )
            logger.debug("工具第 %d 轮完成，%d 个调用", index + 1, len(out.tool_calls))

        if not finished or not text.strip():
            # 两种收不住的情况：轮次用尽还在调；或者它只吐了个工具调用没给正文。
            # 兜底一定不带 tools —— 带着的话它可能继续调，永远说不完。
            messages.append({"role": "user", "content": FORCE_FINISH})
            text = self.llm.chat(messages, tools=None).content

        return text, {"tools": calls, "rounds": index + 1}

    # ================================================================ 发
    def _send(self, chat_id: str, reply: str) -> bool:
        """切分发送。返回是否**每条**气泡都送达（没接入层算 True，命令行不发送）。"""
        if self.bridge is None:
            return True
        reply = reply or ""
        if bool(self.cfg.get("wechat.split_reply", True)):
            bubbles = split_reply(
                reply,
                max_chars=int(self.cfg.get("wechat.bubble_chars", 40)),
                max_bubbles=int(self.cfg.get("wechat.max_bubbles", 3)),
            )
        else:
            # 关掉切分就整段一次发（会显得不像人，但有人就想要这个）
            bubbles = [reply.strip()] if reply.strip() else []
        per_char = float(self.cfg.get("wechat.typing_per_char", 0.035))
        all_ok = True
        for index, bubble in enumerate(bubbles):
            if index:
                time.sleep(min(2.5, per_char * len(bubble) + 0.4))
            if not self._send_one(chat_id, bubble):
                all_ok = False
                # 完整原文进日志，别只留前 40 字：要能照着补发
                logger.error("未送达（已重试 2 次，放弃）：%r -> %s", bubble, chat_id)
        # 「我们这层发了几条、成没成」必须留痕：库内部重发时这一行是唯一的对照物
        logger.info("发送完成：%d 条气泡 → %s（%s）", len(bubbles), chat_id,
                    "全部送达" if all_ok else "有未送达")
        return all_ok

    def _send_one(self, chat_id: str, bubble: str) -> bool:
        """发一条气泡，失败按 2s / 5s 各重试一次。

        发送是整套方案最脆的一环（要驱动微信界面），一次 UIA 抖动就丢一句话。
        实现方案承诺的就是「重试两次后留档放弃」。重试会占住当前会话的锁，
        所以次数不能多——宁可留档让人补，也不能把会话线程堵死。
        """
        for attempt, delay in enumerate((0.0, 2.0, 5.0)):
            if delay:
                time.sleep(delay)
            self._respect_cooldown(chat_id)
            if self.bridge.send_text(chat_id, bubble):
                logger.debug("气泡已发出（第 %d 次尝试）：%r -> %s",
                             attempt + 1, bubble[:30], chat_id)
                if attempt:
                    logger.info("第 %d 次重试，发出去了：%s", attempt, bubble[:30])
                return True
            logger.warning("发送失败（第 %d 次）：%r -> %s",
                           attempt + 1, bubble[:40], chat_id)
        return False

    def _respect_cooldown(self, chat_id: str) -> None:
        """两条消息之间至少隔 cooldown 秒。

        合并窗口只管「连着发来好几条」，管不住「刚回完又有人 @」——
        没有这条限制时，短时间内连发会让微信界面抢焦点，看着像刷屏。
        """
        cooldown = max(0.0, float(self.cfg.get("wechat.cooldown", 0) or 0))
        if cooldown <= 0:
            return
        wait = cooldown - (time.time() - self._last_sent.get(chat_id, 0.0))
        if wait > 0:
            time.sleep(min(wait, cooldown))
        self._last_sent[chat_id] = time.time()
        # 长期跑下来这个 dict 会一个会话一条地长；超过阈值就只留最近一小时的
        if len(self._last_sent) > 512:
            cutoff = time.time() - 3600
            self._last_sent = {
                key: value for key, value in self._last_sent.items() if value >= cutoff
            }

    def _send_voice(self, chat_id: str, texts: List[str]) -> None:
        """把排队的语音发出去。

        微信 4.x 发不了语音气泡，这里发的是 mp3 文件——对方点开就能听，内容一样。
        合成失败或发送失败都只记日志，不重试、不打扰对方：文字回复已经发出去了。
        """
        if not texts or self.bridge is None or self.tts is None:
            return
        limit = max(1, int(self.cfg.get("voice.max_per_turn", 1)))
        for text in texts[:limit]:
            path = self.tts.synth(text)
            if path is None:
                logger.warning("语音合成失败，这条不发了：%s", text[:24])
                continue
            if self.bridge.send_file(chat_id, str(path)):
                logger.info("已发语音：%s", path.name)
            else:
                logger.error("语音没发出去：%s -> %s", path.name, chat_id)

    # ================================================================ 指令
    def _command(self, chat_id: str, text: str) -> Optional[str]:
        """识别指令。命中就返回要说的话，没命中返回 None 走正常流程。"""
        word = (text or "").strip().lower()
        if word in ("/reset", "/重置", "/清空"):
            # 只清这轮对话的上下文，不碰记忆库：记忆是长期的事，
            # 「清一下上下文」不该被理解成「忘掉我说过的一切」。
            self._turns.pop(chat_id, None)
            return "上下文清空了。记忆没动，该记着的还记着。"
        if word in ("/help", "/帮助", "/指令"):
            return "能用的就这么几个：\n/reset 清空这轮对话的上下文（记忆不动）\n/help 看这个"
        return None

    # ================================================================ 内部
    def _passes_filter_legacy(self, chat_id: str, text: str, mode: str = "auto") -> bool:
        """旧判定：只有 @ 我或命中群关键词才回。auto 模式一律回。

        只在 `engage.enabled=false`（软回退）或 `__new__` 建的 Bot 没有 GroupEngager 时走。
        新判定统一走 `_judge` -> `GroupEngager.judge`。
        """
        if mode != "at":
            return True
        at_me = bool(self.me) and ("@" + self.me) in text
        keywords = [str(k).lower() for k in (self.cfg.get("wechat.group_keywords") or [])]
        return at_me or any(k in text.lower() for k in keywords)

    def _group_members(self, chat_id: str):
        """读群成员表（给 MemberCache 当 provider）。

        没有接入层就抛异常——provider 抛异常是**预期**的：MemberCache 会 catch 并
        缓存成 None，于是「@ 排除法」这一轮自动不启用，只认精确匹配，保守不插嘴。
        """
        if getattr(self, "bridge", None) is None:
            raise RuntimeError("没有接入层，读不了群成员")
        return self.bridge.db.get_group_members(chat_id)

    def _self_names(self, chat_id: str) -> List[str]:
        """机器人自己的全部名字：账号昵称 + 手动别名 + 本群成员表里自己那条。"""
        names = [self.me] if self.me else []
        names += [str(x) for x in (self.cfg.get("people.self_names") or [])]
        members = getattr(self, "members", None)
        bridge = getattr(self, "bridge", None)
        if members is not None and bridge is not None:
            try:
                names += members.self_names(chat_id, bridge.self_wxid)
            except Exception:  # noqa: BLE001
                # 读成员表失败只少几个别名，不影响「账号昵称/手动别名」这两路
                pass
        return names

    def _persona_names(self) -> List[str]:
        """角色名也算机器人自己的名字（换个角色叫，也要应）。"""
        return self.personas.names()

    def _judge(self, chat_id: str, msg: Dict[str, Any], text: str, mode: str):
        """判定这条群消息回不回，返回 (pass, verdict)。

        私聊 / auto 模式不参与打分，一律放行；at 模式才走 GroupEngager。
        blocked 必须在这里 return False——它要在 `_seen.add` 和 `_pending` **之前**
        被拦掉，否则会给「完全不处理」的人留下指纹和合并队列。
        """
        if not is_group(chat_id) or mode != "at":
            return True, None
        sender = str(msg.get("sender") or "")
        sender_name = str(msg.get("sender_name") or "")
        people = getattr(self, "people", None)
        level = people.level_of(sender, sender_name) if people is not None else "stranger"
        if level == "blocked":
            return False, None
        engager = getattr(self, "engager", None)
        if engager is None:
            # 测试用 __new__ 建的 Bot 没有 engager，退回旧逻辑
            return self._passes_filter_legacy(chat_id, text, mode), None
        bridge = getattr(self, "bridge", None)
        verdict = engager.judge(
            chat_id=chat_id,
            sender=sender,
            sender_name=sender_name,
            text=text,
            is_group=True,
            self_names=self._self_names(chat_id),
            self_wxid=bridge.self_wxid if bridge is not None else "",
            persona_names=self._persona_names(),
        )
        if verdict.hit is None:
            # engage.enabled=false：软回退到旧判定
            return self._passes_filter_legacy(chat_id, text, mode), verdict
        return bool(verdict.hit), verdict

    def _trim_turns(self, turns: List[Dict[str, Any]], limit: int) -> None:
        if len(turns) > limit:
            del turns[: len(turns) - limit]


def estimate_messages(messages: List[Dict[str, Any]]) -> int:
    """粗略估一串消息占多少 token（含每条消息的固定开销）。

    工具循环里每轮都会往 messages 追加，不量一下就不知道离窗口上限还有多远；
    估得粗一点没关系，宁可早停一轮，也别让接口报「上下文超长」。
    """
    total = 0
    for msg in messages or []:
        text = str(msg.get("content") or "")
        if msg.get("tool_calls"):
            text += " ".join(
                str(call.get("function", {}).get("arguments") or "")
                for call in msg.get("tool_calls") or []
            )
        total += estimate_tokens(text) + 4
    return total


def _clip_result(text: str, remaining: int) -> tuple:
    """按剩余预算截断一条工具结果。

    `Registry.call` 已经把单条截到 8000 字符，但一次回复里可能连着调四五个工具，
    累加照样能把上下文撑爆——闸门只管拼装那一刻，管不到循环里追加的东西。

    Args:
        text: 工具返回的原文。
        remaining: 本轮的工具体预算还剩多少 token。

    Returns:
        tuple[str, int]: （可能被截断的文本, 这条实际占了多少 token）
    """
    text = text or ""
    if remaining <= 0:
        return "（工具结果预算用完了，这一条不再展开）", 0
    cost = estimate_tokens(text)
    if cost <= remaining:
        return text, cost

    keep = _fit_chars(text, remaining)
    clipped = text[:keep].rstrip() + "\n\n（结果太长，已截断）"
    return clipped, estimate_tokens(clipped)


def _fit_chars(text: str, max_tokens: int) -> int:
    """二分都不用：按字符/token 比例粗估后逐步收紧，最多迭代十来次。"""
    per_token = len(text) / max(1, estimate_tokens(text))
    keep = max(0, min(int(max_tokens * per_token * 0.9), len(text)))
    while keep > 0 and estimate_tokens(text[:keep]) > max_tokens:
        keep = int(keep * 0.85)
    return keep


# 工具失败时 Registry 会回一句「（……）」，用前缀判断成败，不去猜异常类型
_FAIL_MARKS = ("（工具执行失败", "（工具参数", "（没有名为", "（工具结果预算用完")


def _tool_ok(result: str) -> bool:
    """工具这次算不算成功。只给面板统计用，不影响回给模型的文本。"""
    return not (result or "").startswith(_FAIL_MARKS)


def _apply_log_level(level: str) -> None:
    """日志级别热改。root 的 level 一变，所有子 logger 跟着走。"""
    value = getattr(logging, (level or "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    if root.level != value:
        root.setLevel(value)
        logging.getLogger(__name__).info("日志级别改为 %s", level.upper())


class _Seen:
    """处理过的消息指纹。持久化，重启后不会把老消息再回一遍。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: List[str] = []
        self._set: set = set()
        self._lock = threading.Lock()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                self._items = [str(x) for x in data][-SEEN_CAP:]
                self._set = set(self._items)
        except Exception:  # noqa: BLE001
            self._items, self._set = [], set()

    def add(self, key: str) -> bool:
        """没处理过就记下并返回 True；处理过返回 False。"""
        with self._lock:
            if key in self._set:
                return False
            self._items.append(key)
            self._set.add(key)
            if len(self._items) > SEEN_CAP:
                drop = self._items[: len(self._items) - SEEN_CAP]
                self._items = self._items[-SEEN_CAP:]
                self._set -= set(drop)
            self._save()
            return True

    def discard(self, key: str) -> None:
        """撤销一条指纹（处理失败时用）。

        指纹原本在处理之前就登记了，好处是重启后不会把老消息再答一遍；
        代价是「登记完就崩」的那条永远不会被补。处理失败时撤掉它，
        下次再投递就能重新走一遍——宁可重复，不可漏。
        """
        with self._lock:
            if key not in self._set:
                return
            self._set.discard(key)
            try:
                self._items.remove(key)
            except ValueError:
                pass
            self._save()

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._items), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            # 不再静默：写不进去意味着重启后可能把老消息再答一遍，
            # 这属于要能从日志里查出来的降级，不是可以 pass 掉的小事。
            logger.warning("seen.json 写不进去（重启后可能重复回复）：%s", exc)


def _msg_key(msg: Dict[str, Any], text: str) -> str:
    raw = msg.get("raw") or {}
    for field in ("local_id", "server_id", "sort_seq"):
        value = raw.get(field)
        if value not in (None, "", 0):
            return f"{msg.get('chat_id')}|{field}|{value}"
    return f"{msg.get('chat_id')}|{msg.get('time')}|{text[:40]}"
