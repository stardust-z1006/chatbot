# -*- coding: utf-8 -*-
"""微信接入层：读库、监听、发送。

分成两条通道，互不干扰：
  - 收消息 → WeChatDB + Listener（纯读本地库，不碰界面，稳、快、不抢焦点）
  - 发消息 → WeChat 的 GUI 通道（微信 PC 端没有开放 API，只能驱动界面）

所有 wechatauto 的调用都收在这个文件里，换一套接入方案时只改这里。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_booted = False

# 同一句话在这个窗口内、同一个线程里又要求发一次，就当成「库自己在重发」。
# 10 秒来自实测：库内重发被拟人节流拖过，间隔 6~8 秒很常见。
DEFAULT_DEDUPE_SECONDS = 10.0

# UIA 盲区里回读落库的次数与间隔：**自己控时，不用库那套 1s×8 的轮询**。
# 为什么：`_wait_landed` 跑在 `click_send` 里 → 在 `send_text` 的 `_send_lock`
# （Bridge 全局锁，不是按会话）里 → 一次 `send_text` 最多 4 次 `click_send`，
# 8s×4=32s，再乘上 Bot 的三次重试就是 70 秒级，这段时间**所有会话**都被堵住；
# 而「停一分钟收不到消息」比「多发一条」严重得多。这里 3 次 × 0.4s（含读库
# 约 1.0~1.5 秒封顶），微信写库（WAL）一般一次就读得到，读不到就交给库重试 /
# Bot 层的 2s、5s 重试。空会话拍不到水位时整层跳过，不做无依据的猜测。
LANDED_TRIES = 3
LANDED_GAP = 0.4

# wechatauto 返回的是微信库里的中文类型名（见 db.py 的 _MSG_TYPE_NAMES），
# 上层只认英文短代号，所以在这里统一转一次。没列出的一律保留原文，
# 上层对不认识的类型自然就跳过了。
_TYPE_MAP = {
    "文本": "text",
    "图片": "image",
    "语音": "voice",
    "视频": "video",
    "动画表情": "emoji",
    "文件/链接/卡片": "link",
    "音视频通话": "call",
    "系统消息": "system",
}


def _boot_wechatauto():
    """导入 wechatauto 并把它按我们的规矩安置好。

    这个库导入时会做两件不太友好的事：
      - 清空 root logger 的所有 handler（我们的控制台/文件日志会随之失效）
      - 在当前目录建 wechatauto_logs/ 放它自己的日志（把项目根目录搞乱）
    所以这里导入后立刻关掉它的文件日志，并把 handler 补回来，全部写进 data/bot.log。
    """
    global _booted

    import wechatauto

    if not _booted:
        from wechatauto.param import WxParam

        WxParam.ENABLE_FILE_LOGGER = False
        _booted = True

    from app.logging_setup import ensure as _ensure_logging

    _ensure_logging()
    return wechatauto


def _norm_text(text: str) -> str:
    """归一化：只折叠空白（含首尾），标点一个字都不动。

    折叠空白是为了让「粘贴时多一个换行」这种差异落到同一个 key 上；
    标点必须保留——「好。」和「好？」是两句不同的话，不能互相吞掉。
    """
    return " ".join((text or "").split())


class Bridge:
    def __init__(self, cfg=None):
        self.cfg = cfg
        self._db: Any = None
        self._wx: Any = None
        self._listener: Any = None
        self._send_lock = threading.Lock()
        self._self_info: Dict[str, Any] = {}
        # 发送台账：{(会话, 归一化文本): (时间戳, 调用令牌)}。只用于识别
        # 「同一次发送调用里，库把同一句话又写了一遍」，不是业务层的消息去重。
        self._sent_ledger: Dict[Tuple[str, str], Tuple[float, int]] = {}
        self._ledger_lock = threading.Lock()
        # 调用令牌自增源：同一次 send_text 调用内的所有写动作共享一个令牌。
        self._send_seq = 0
        # 人工触发的诊断发送（自检）要能绕过护栏：自检本来就会重复发同一句话，
        # 被拦下来只会让「发送测试消息」这一项假成功。
        self._guard_bypass = threading.local()
        self._read_fail_logged = False

    # ------------------------------------------------------------ 底层对象
    @property
    def db(self) -> Any:
        if self._db is None:
            _boot_wechatauto()
            from wechatauto.db import WeChatDB

            self._db = WeChatDB()
        return self._db

    def _wechat(self) -> Any:
        if self._wx is None:
            _boot_wechatauto()
            from wechatauto import WeChat

            logger.info("初始化微信界面通道…")
            self._wx = WeChat()
            self._install_send_guard(self._wx)
        return self._wx

    # ------------------------------------------------------------ 发送护栏
    def dedupe_seconds(self) -> float:
        """重复发送拦截的开关 + 台账保留时长（秒）。0 = 关掉这层保护。

        每次现读 cfg 而不是缓存：面板改完下一条消息就按新值走，不用重建接入层。
        """
        if self.cfg is None:
            return DEFAULT_DEDUPE_SECONDS
        try:
            value = float(
                self.cfg.get("wechat.send_dedupe_seconds", DEFAULT_DEDUPE_SECONDS)
            )
        except (TypeError, ValueError):
            return DEFAULT_DEDUPE_SECONDS
        return max(0.0, value)

    def _call_token(self) -> int:
        """领一个「本次 send_text 调用」的令牌。

        令牌是这条护栏的主判据：一次 `send_text` 里库最多会写 3~6 次
        （`send_msg` 的 3 轮、`click_send` 的 3 轮、UIA 补点一次），它们共享
        一个令牌；而**两次独立的发送**必然是两个令牌。

        用自增整数而不是 `object()`：令牌要落进台账参与比较，整数永远不会
        撞号、也不占内存；对象则要额外持有引用才敢保证身份唯一。
        """
        with self._ledger_lock:
            self._send_seq += 1
            return self._send_seq

    def _ledger_hit(self, chat: str, text: str) -> str:
        """这句话要不要拦。返回命中的原因（空串 = 不拦，调用方拿它写日志）。

        主判据是**同一次 `send_text` 调用的令牌相同**，不是「N 秒内文本相同」。
        这一点被 QA 打回来改过，原因值得留档：

        `split_reply` 真能切出内容完全相同的 2~3 个气泡（每段 21 字以上、用换行或
        句号分隔即可），`_send` 会在同一个线程里隔 1~2 秒依次发。按「窗口内同文本」
        去重会把第 2、3 个气泡**静默吞掉**（还返回 True，面板全绿），比原来的
        「发两遍」更难发现；而按令牌判，它们是多次独立调用，各自令牌不同，全部放行。
        同时这也顺手消掉「两个独立回复撞上同一个线程号被误吞」的尾部风险。

        `wechat.send_dedupe_seconds` 退化成**兜底**：只有在「本次调用没有令牌」
        且「上一条记录也没有令牌」时才回退成「同线程 + 窗口」，属防御性路径，
        正常流程走不到。
        """
        if not text:
            return ""
        window = self.dedupe_seconds()
        if window <= 0:
            return ""
        key = (chat, _norm_text(text))
        token = getattr(self._guard_bypass, "token", None)
        now = time.time()
        with self._ledger_lock:
            entry = self._sent_ledger.get(key)
        if entry is None:
            return ""
        stamp, mark_token, mark_thread = entry
        if token is not None:
            # 正常流程：只认「同一次调用内」，与过了多久无关（拟人节流可能拖 60 秒）
            return "同一次调用内重发，token 命中" if mark_token == token else ""
        if mark_token is not None:
            # 有令牌的记录不能拿来判「无令牌的调用」——证不了是重发，宁可放行
            return ""
        if mark_thread == threading.get_ident() and 0.0 <= now - stamp <= window:
            return "窗口兜底：无令牌 + 同线程 + %gs 内同文本" % window
        return ""

    def _ledger_mark(self, chat: str, text: str) -> None:
        """记一笔「这句话本次调用里真的写出去过」。只记成功，失败不记。

        只记成功，是为了不动我们自己那层的重试语义：`Bot._send_one` 的 2s/5s
        重试发生在「库判失败之后」，那时这里没有记账，重试照旧会去试。
        """
        if not text:
            return
        key = (chat, _norm_text(text))
        now = time.time()
        token = getattr(self._guard_bypass, "token", None)
        with self._ledger_lock:
            self._sent_ledger[key] = (now, token, threading.get_ident())
            if len(self._sent_ledger) > 256:
                # 长期跑下来会一个会话一句话地长，超过阈值就按窗口清一遍
                cutoff = now - max(self.dedupe_seconds(), 60.0)
                self._sent_ledger = {
                    k: v for k, v in self._sent_ledger.items() if v[0] >= cutoff
                }

    def _input_text_now(self, gui: Any) -> Optional[str]:
        """读聊天输入框里现在到底是什么字。读不到返回 None（不是"空"）。

        这是和库里像素探针**互相独立**的第二条证据：探针数的是「输入框那块区域
        有没有深色像素」，会被光标、残留气泡、下方工具栏一起带偏；这里读的是控件
        的值本身。

        读不到时第一次记 INFO（进程内只记一次）：这条护栏在 OCR-only 模式下等于
        没装，出问题时要能一眼看出是这个原因，不能让「读不到」悄悄退化成 DEBUG。
        """
        try:
            uia = getattr(gui, "_get_uia", None)
            uia = uia() if callable(uia) else None
            if uia is None:
                self._note_read_fail("UIA 引擎不可用（OCR-only 模式）")
                return None
            ctrl = uia._chat_input()
            if ctrl is None:
                self._note_read_fail("拿不到聊天输入框控件")
                return None
            return uia._read_edit_value(ctrl)
        except Exception as exc:
            self._note_read_fail(f"读输入框抛异常：{exc}")
            return None

    def _note_read_fail(self, reason: str) -> None:
        """「读不到输入框内容」只记一次 INFO，之后降到 DEBUG，避免每句话刷屏。"""
        if not self._read_fail_logged:
            self._read_fail_logged = True
            logger.info("读不到输入框内容（%s）：本进程内发送护栏的「假阴性修正」"
                        "不生效，改用「库里有没有这条新消息」这层地面真值", reason)
        else:
            logger.debug("读不到输入框内容：%s", reason)

    def _db_watermark(self, gui: Any, who: str) -> Optional[Dict[str, Any]]:
        """拍一个「这次写之前」的落库水位，只给 UIA 盲区用。

        为什么必须带 `who`：库里是按会话查的，传 None 会退化成「查自己/当前会话」，
        同一句话在别的会话里存在就会被当成这次发成功了——宁可这层证据取不到，
        也不能查错会话把真失败判成成功。
        """
        try:
            return gui._send_watermark(who)
        except Exception as exc:
            logger.debug("拍发送水位失败（这层证据本次不用）：%s", exc)
            return None

    def _landed_in_db(self, gui: Any, text: str, who: str,
                      mark: Dict[str, Any]) -> bool:
        """回读微信库：水位之后有没有「正文逐字相等」的新消息落进来。

        判成功的三个门槛缺一不可：只认水位之后的新行（旧消息不算）、只认自己发的
        （`sender_id == 2`，库里判）、正文逐字相等（`mode='exact'`，不放宽成包含匹配）。
        所以它**不会**把「上次发过同样的话」或「别的会话里的同文本」当成这一次成功；
        取不到证据时一律返回 False，交给库继续重试——绝不因为「可能发了」就判成功。

        轮询时自己控（`LANDED_TRIES × LANDED_GAP`），不走 `_wait_landed` 那套
        1s×8：它跑在全局 `_send_lock` 里，8 秒上限会把所有会话的发送一起堵住。
        读不到就早退，让外层重试来兜——反正库有 3 轮、`Bot._send_one` 还有 2s/5s。
        """
        checker = getattr(gui, "_verify_sent", None)
        if callable(checker):
            for attempt in range(LANDED_TRIES):
                try:
                    if checker(text, who, mode="exact", after=mark):
                        return True
                except Exception as exc:
                    logger.debug("回读落库失败（按没发出去处理）：%s", exc)
                    return False
                if attempt + 1 < LANDED_TRIES:
                    time.sleep(LANDED_GAP)
            return False
        # 老版本库/假件只有 `_wait_landed`：退回它（语义一致，只是它自己控时）。
        waiter = getattr(gui, "_wait_landed", None)
        if not callable(waiter):
            return False
        try:
            return bool(waiter(text, who, mark))
        except Exception as exc:
            logger.debug("回读落库失败（按没发出去处理）：%s", exc)
            return False

    def _install_send_guard(self, wx: Any) -> None:
        """给界面通道装一道发送护栏（只作用于本进程的这个实例，幂等）。

        为什么需要它（2026-10-27「一条回复发两遍」的根因，证据见交接）：
        `WeChatGUI.click_send` 判定「发出去没有」用的是像素探针
        `_input_box_has_text`。回车之后探针若仍报「输入框有字」（光标、上一次的
        残留、下方工具栏都算），它就认为没发出去；而 `WeChatGUI.send_msg` 外层的
        `for attempt in range(3)` 会**重新粘贴同一段文本再回车一次**。于是同一次
        调用写出两条一模一样的消息，我们这层看到的却是「一次成功、零重试」——
        日志里一点痕迹都没有。

        护栏只做三件事，且都不改变成功判定的方向：
          1. 库说「输入框没清空」时，用 UIA 读一次真实内容作第二意见：发之前框里
             有字、发之后框里空了 → 那一下确实发出去了，按成功返回，不让它重发。
          2. 同一次 `send_text` 调用里（同一个调用令牌）同一句话又要写一遍 → 拦掉
             并记日志。**主判据是「同一次调用」，不是「N 秒内同文本」**：后者会把
             `split_reply` 切出来的多个相同气泡当重发吞掉（用户会少收到内容）。
             `wechat.send_dedupe_seconds` 只在「没有令牌」的防御性路径上兜底。
          3. UIA 读不到输入框时（这台机器上「UIA 子控件不可见」很常见），改问微信
             自己的数据库：写之前拍水位，写之后回读（自己控时，最多 3×0.5s，绝不
             用库那套 8 秒轮询把全局发送锁堵住），只认水位之后、自己发的、正文
             逐字相等的新行。这是三层证据里最硬的一层，也是唯一在盲区还能判真假
             的一层——正因为它只认「水位之后的新行 + 精确正文」，所以不会把「上次
             发过同样的话」当成这一次；取不到证据就返回 False，绝不「宁可少发」。
             注意：新会话第一句拿不到水位（库里还没行），这层会自动跳过。
        证据不足（UIA 没起来、控件不暴露值、库也读不到）时一律退回库原来的结论。
        """
        gui = getattr(wx, "_gui", None) or getattr(wx, "gui", None)
        if gui is None or getattr(gui, "_bot_send_guard", False):
            return
        original = getattr(gui, "click_send", None)
        if not callable(original):
            logger.warning("界面通道没有 click_send，发送护栏装不上（发送照旧）")
            return

        def click_send(fast: bool = False) -> bool:
            if getattr(self._guard_bypass, "on", False):
                return original(fast=fast)
            text = str(getattr(gui, "_pending_text", "") or "").strip()
            chat = str(getattr(gui, "_current_chat", "") or "")
            reason = self._ledger_hit(chat, text)
            if reason:
                logger.info("疑似重复发送，已跳过（%s，%d 字）：%r → %s",
                            reason, len(text), text[:30], chat or "当前会话")
                # 待发文本一次性消费：留着会让下一次正常发送被误判成「内容不符」
                gui._pending_text = None
                return True
            before = self._input_text_now(gui) if text else None
            # 第三层证据的「前提」：只有 UIA 处于盲区（读不到输入框内容）时才去拍
            # 库水位——正常路径一次库都不读，不给每句话加几百毫秒。
            mark = None
            if (text and before is None and chat
                    and hasattr(gui, "_send_watermark")
                    and (hasattr(gui, "_verify_sent") or hasattr(gui, "_wait_landed"))):
                mark = self._db_watermark(gui, chat)
            if original(fast=fast):
                self._ledger_mark(chat, text)
                return True
            after = self._input_text_now(gui) if text else None
            if before and after is not None and not after.strip():
                logger.info("发前输入框有字、发后已空，这一下确实发出去了，不再重发"
                            "（%d 字）：%r → %s", len(text), text[:30],
                            chat or "当前会话")
                self._ledger_mark(chat, text)
                return True
            if mark is not None and self._landed_in_db(gui, text, chat, mark):
                logger.info("UIA 读不到输入框，但库里确实多了这条新消息，判为已发出、"
                            "不再重发（%d 字）：%r → %s", len(text), text[:30], chat)
                self._ledger_mark(chat, text)
                return True
            return False

        gui.click_send = click_send
        gui._bot_send_guard = True
        logger.info("已装发送护栏：同一次发送调用内不重发同一句"
                    "（开关 wechat.send_dedupe_seconds，0 = 关）")

    # ------------------------------------------------------------ 账号信息
    def self_info(self) -> Dict[str, Any]:
        if not self._self_info:
            info = self.db.get_self_info()
            self._self_info = {
                "username": info.get("username") or "",
                "nickname": info.get("nick_name") or info.get("nickname") or "",
            }
        return self._self_info

    @property
    def self_wxid(self) -> str:
        return self.self_info()["username"]

    def display_name(self, username: str) -> str:
        """wxid → 界面上显示的名字（备注优先）。发送时必须用显示名。"""
        if not username:
            return username
        if username in ("filehelper", "文件传输助手"):
            return "文件传输助手"
        try:
            nick = self.db.get_nickname(username)
            if nick and nick != username:
                return nick
        except Exception:
            pass
        return username

    def resolve(self, name: str) -> Optional[str]:
        """昵称/备注 → wxid。"""
        name = (name or "").strip()
        if not name:
            return None
        if name.endswith("@chatroom") or name.startswith(("wxid_", "gh_")):
            return name
        if name in ("文件传输助手", "filehelper"):
            return "filehelper"
        try:
            for hit in self.db.search_contact(name):
                if name in (hit.get("nick_name"), hit.get("remark")):
                    return hit["username"]
        except Exception:
            pass
        try:
            for user, nick in (self.db.nickname_map() or {}).items():
                if nick == name:
                    return user
        except Exception:
            pass
        return None

    def nickname(self, username: str) -> str:
        return self.display_name(username)

    def sessions(self, limit: int = 30) -> List[Dict[str, Any]]:
        try:
            return self.db.get_sessions(limit=limit)
        except Exception as exc:
            logger.warning("读取会话列表失败：%s", exc)
            return []

    # ------------------------------------------------------------ 监听
    def listen(
        self,
        names: List[str],
        on_message: Callable[[Dict[str, Any]], None],
        interval: float = 1.0,
    ) -> Any:
        """注册监听。

        names 为空 → 监听全部会话（运行中自动发现新会话）。
        回调在工作线程里跑，每个会话一条串行队列。
        """
        _boot_wechatauto()
        from wechatauto.db import Listener

        listener = Listener(self.db, interval=interval)
        if names:
            for raw in names:
                username = self.resolve(raw) or raw
                listener.add_listener(username, lambda msg, _lst, u=username: on_message(
                    self._adapt(msg, u)
                ))
                logger.info("监听会话：%s", self.display_name(username))
        else:
            listener.add_all(
                lambda msg, _lst: on_message(self._adapt(msg, msg.get("username", ""))),
                discover=True,
            )
            logger.info("监听全部会话（自动发现新会话）")
        self._listener = listener
        listener.start()
        return listener

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def _adapt(self, msg: Dict[str, Any], username: str) -> Dict[str, Any]:
        sender = msg.get("sender_username") or ""
        is_self = bool(sender and self.self_wxid and sender == self.self_wxid)
        if not sender and msg.get("sender_id") == 2:
            is_self = True
        raw_type = str(msg.get("type") or "")
        return {
            "chat_id": username,
            "chat_name": self.display_name(username) or username,
            "sender": sender,
            "sender_name": self.display_name(sender) if sender else "",
            "is_self": is_self,
            "type": _TYPE_MAP.get(raw_type, raw_type.lower() or "unknown"),
            "type_raw": raw_type,
            "content": str(msg.get("content") or ""),
            "time": int(msg.get("create_time") or 0),
            "raw": msg,
        }

    # ------------------------------------------------------------ 窗口状态
    def ensure_ready(self) -> None:
        """提前把界面通道建好（幂等）。

        建了之后 window_state() 才有意义，面板才能一开始就看到窗口是不是被最小化了。
        """
        try:
            self._wechat()
        except Exception as exc:
            raise RuntimeError(f"微信界面通道建不起来：{exc}") from exc

    def window_state(self) -> Dict[str, Any]:
        """微信窗口现在能不能发消息。

        真·最小化时 Windows 会把窗口挪到 -32000 附近，UIA 敲不进输入框，
        wechatauto 报「内容未落进输入框（相似度 0.00）」——消息就丢了。
        这里把状态透出来，面板和 doctor 都能提前看到「发不出去」的原因。
        """
        out: Dict[str, Any] = {"hwnd": 0, "state": "unknown", "visible": False,
                               "iconic": False, "foreground": False}
        if self._wx is None:
            return out   # 还没建界面通道；不为了看一眼就去初始化它
        gui = getattr(self._wx, "_gui", None) or getattr(self._wx, "gui", None)
        out["hwnd"] = int(getattr(gui, "main_hwnd", 0) or 0)
        if not out["hwnd"]:
            return out
        try:
            user32 = ctypes.windll.user32
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(ctypes.c_void_p(out["hwnd"]), ctypes.byref(rect))
            offscreen = rect.left <= -10000 or rect.top <= -10000
            visible = bool(user32.IsWindowVisible(ctypes.c_void_p(out["hwnd"])))
            iconic = bool(user32.IsIconic(ctypes.c_void_p(out["hwnd"])))
            fg = int(user32.GetForegroundWindow() or 0)
            out.update(
                visible=visible,
                iconic=iconic,
                foreground=(fg == out["hwnd"]),
                rect=[rect.left, rect.top, rect.right, rect.bottom],
                state=("minimized" if (iconic or offscreen)
                       else ("hidden" if not visible else "normal")),
            )
        except Exception as exc:
            out["error"] = str(exc)
        return out

    def _ensure_window(self) -> None:
        """发之前把窗口弄到能用的状态。

        只在「最小化了 / 不可见」时才动手：bring_to_front() 会抢一次前台，
        每次发送都调的话，用户正在别的窗口打字会被不停打断。
        """
        try:
            state = self.window_state()
        except Exception as exc:
            logger.debug("看窗口状态失败，照常发：%s", exc)
            return
        if state.get("state") in ("normal", "hidden", "unknown"):
            return
        gui = getattr(self._wx, "_gui", None) or getattr(self._wx, "gui", None)
        if gui is None:
            return
        logger.info("微信窗口%s，先还原再发（最小化时 UIA 敲不进输入框）",
                    "最小化了" if state.get("iconic") else "不在屏幕上")
        # 先试「还原但不抢焦点」：把窗口从最小化放回屏幕，不打断用户正在做的事。
        # 不行再走 bring_to_front()（会抢一次前台，但至少能发出去）。
        try:
            hwnd = int(state.get("hwnd") or 0)
            if hwnd:
                ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), 4)  # SW_SHOWNOACTIVATE
                time.sleep(0.5)
            if self.window_state().get("state") == "normal":
                return
        except Exception as exc:
            logger.debug("静默还原失败，改走置前：%s", exc)
        try:
            gui.bring_to_front()
            time.sleep(0.6)
        except Exception as exc:
            logger.warning("还原微信窗口失败，仍尝试发送：%s", exc)

    # ------------------------------------------------------------ 发送
    def send_text(self, chat_id: str, text: str, guard: bool = True) -> bool:
        """发一条文本。

        Args:
            guard: 是否受发送护栏约束。只有「人工触发的诊断发送」（自检）才传
                False——那种发送本来就会重复发同一句话，被护栏拦下反而会让
                自检报假成功。
        """
        text = (text or "").strip()
        if not text or not chat_id:
            return False
        target = self.display_name(chat_id)
        started = time.time()
        try:
            with self._send_lock:
                # 顺序要紧：先建界面通道，_ensure_window 才看得到窗口状态
                # （反过来它会拿到 unknown，等于没检查）
                self._wechat()
                self._ensure_window()
                previous = getattr(self._guard_bypass, "on", False)
                previous_token = getattr(self._guard_bypass, "token", None)
                self._guard_bypass.on = previous or not guard
                # 令牌就是「这一次 send_text 调用」的身份：里面所有的重发共享它，
                # 外面任何一次新调用都会拿到新令牌（护栏只认令牌，不认时间窗）。
                self._guard_bypass.token = self._call_token()
                try:
                    result = self._wechat().SendMsg(text, who=target)
                finally:
                    self._guard_bypass.on = previous
                    self._guard_bypass.token = previous_token
            ok = bool(getattr(result, "is_success", True))
            if not ok:
                logger.warning("发送失败 → %s：%s", target, result)
            else:
                # 成功也留一行：出问题时「我们以为发了几次」必须能和微信库里
                # 的真实条数对上，否则排查只能靠猜（这次就吃过这个亏）。
                logger.debug("已发送 → %s（%.1fs）：%r", target,
                             time.time() - started, text[:30])
            return ok
        except Exception as exc:
            logger.error("发送异常 → %s：%s", target, exc)
            return False

    def send_file(self, chat_id: str, path: str) -> bool:
        target = self.display_name(chat_id)
        try:
            with self._send_lock:
                self._wechat()
                self._ensure_window()
                result = self._wechat().SendFiles(path, who=target)
            ok = bool(getattr(result, "is_success", True))
            if not ok:
                logger.warning("发文件失败 → %s：%s", target, result)
            return ok
        except Exception as exc:
            logger.error("发文件异常 → %s：%s", target, exc)
            return False

    # ------------------------------------------------------------ 自检
    def selftest(self, target: str = "文件传输助手") -> Dict[str, Any]:
        """发一条消息给「文件传输助手」，把发送通道的每一环都验一遍。

        发送是这套方案里最脆的一环（要驱动微信界面），所以单独做一个自检，
        失败时直接告诉你卡在哪：窗口没开 / 被最小化 / UIA 拿不到 / OCR 缺依赖。
        """
        _boot_wechatauto()
        report: Dict[str, Any] = {"target": target, "steps": []}

        def step(name: str, ok: bool, detail: str = "") -> None:
            report["steps"].append({"name": name, "ok": ok, "detail": detail})
        # 1. 窗口 —— 必须用和发送路径**同一个判据**（window_state）。
        #    以前这里另搞一套：EnumWindows 扫标题叫「微信 / Weixin」的顶层窗口，
        #    只要有一个不最小化就报「正常」。但发送用的是 gui.main_hwnd，
        #    扫到的可能是小程序窗口或另一个同名窗口，于是自检说正常、
        #    实际在最小化状态下敲不进输入框（2026-10-26 22:15 实测踩过：
        #    自检报「正常」+「发送失败」，命令行直接调 send_text 却成功）。
        #    自检是用户唯一能自己跑的入口，报假失败比不报更糟。
        try:
            self.ensure_ready()
            before = self.window_state()
            if not int(before.get("hwnd") or 0):
                step("微信主窗口", False,
                     "界面通道建起来了但拿不到主窗口句柄，客户端是不是没登录？")
            else:
                self._ensure_window()          # 和真正发送前做的是同一件事
                after = self.window_state()
                detail = f"hwnd={after.get('hwnd')} 状态={after.get('state')}"
                if after.get("state") != "normal":
                    step("微信主窗口", False,
                         detail + "；还原没成功，UIA 敲不进输入框——"
                         "先把微信窗口点出来再跑一次（这条不修等于每条都发不出去）")
                elif before.get("state") != "normal":
                    step("微信主窗口", True,
                         detail + f"（原来是 {before.get('state')}，已自动还原）")
                else:
                    step("微信主窗口", True, detail)
        except Exception as exc:
            step("微信主窗口", False, f"检查失败：{exc}")

        # 2. UIA / OCR 通道
        try:
            from wechatauto.guia import ScreenOCR

            step("OCR 备用通道", ScreenOCR.available(), "可用")
        except Exception as exc:
            step("OCR 备用通道", False, f"不可用：{exc}")

        # 3. 真发一条
        ok = self.send_text("filehelper", "【自检】这条是机器人发的测试消息，收到说明发送通道正常",
                            guard=False)
        step("发送测试消息", ok, "已发到「文件传输助手」" if ok else "发送失败，看上面日志里的原因")
        report["ok"] = all(s["ok"] for s in report["steps"])
        return report

    # ------------------------------------------------------------ 历史
    def recent_messages(self, chat_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        try:
            return self.db.get_messages(chat_id, limit=limit)
        except Exception as exc:
            logger.warning("读历史失败（%s）：%s", chat_id, exc)
            return []
