# -*- coding: utf-8 -*-
"""把一条回复发出去：切分、冷却、重试、语音。

从 `bot.py` 抽出来的原因：编排层关心的是「说什么」，这一段关心的是「怎么送出去」，
两件事的失败模式完全不同（前者是模型/上下文，后者是 UIA 抖动、窗口最小化、焦点被抢）。
混在一个类里的时候，改发送策略要在一堆编排代码里翻。

接缝保持原样：`Bot._send` 仍然是「发一条回复」的唯一入口（它转调这里），
测试和其它模块换掉它就等于换掉整个发送层——`test_concurrency` 正是这么用的。

三条硬约束（改之前先读）：
  1. 一条回复会切成几条气泡，**任何一条失败都要记完整原文**，好照着补发。
  2. 重试只有 2s / 5s 两次：发送期间占着会话锁，次数多了会把整个会话堵死，
     而「停一分钟收不到消息」比「多说一句」严重得多。
  3. 语音是 mp3 文件，不是语音气泡（微信 4.x 没有可用的录音入口）。
     它永远在文字之后发，且失败不重试、不影响文字已经送达这件事。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .wechat.reply_split import split_reply

logger = logging.getLogger(__name__)

# 同一条气泡最多试几次、每次之前等多久。首发不等，之后各等 2s / 5s。
RETRY_DELAYS = (0.0, 2.0, 5.0)
# 气泡之间的停顿上限：再长就不像打字了，像卡住。
MAX_GAP = 2.5
# 冷却表超过这么多会话就顺手清一次（只留最近一小时的）。
COOLDOWN_CAP = 512


class Sender:
    """发送层：切分、冷却、重试、语音。只依赖 cfg / bridge / tts，不碰编排状态。"""

    def __init__(self, cfg: Any,
                 bridge_getter: Callable[[], Any],
                 tts_getter: Optional[Callable[[], Any]] = None) -> None:
        self.cfg = cfg
        # 两个都传 getter，不传对象：bridge 会被测试替换（先建 Bot 再塞假接入层），
        # TTS 会被面板热改重建。存下来的是"那一刻的引用"，之后就会用旧对象干活。
        self._bridge_getter = bridge_getter
        self._tts_getter = tts_getter or (lambda: None)
        self._last_sent: Dict[str, float] = {}

    # ------------------------------------------------------------------ 对外
    def send(self, chat_id: str, reply: str) -> bool:
        """切分发送。返回是否**每条**气泡都送达（没有接入层算 True，命令行不发送）。"""
        bridge = self._bridge_getter()
        if bridge is None:
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
                time.sleep(min(MAX_GAP, per_char * len(bubble) + 0.4))
            if not self._send_one(bridge, chat_id, bubble):
                all_ok = False
                # 完整原文进日志，别只留前 40 字：要能照着补发
                logger.error("未送达（已重试 2 次，放弃）：%r -> %s", bubble, chat_id)
        # 「我们这层发了几条、成没成」必须留痕：库内部重发时这一行是唯一的对照物
        logger.info("发送完成：%d 条气泡 → %s（%s）", len(bubbles), chat_id,
                    "全部送达" if all_ok else "有未送达")
        return all_ok

    def send_voice(self, chat_id: str, texts: List[str]) -> None:
        """把排队的语音发出去。

        微信 4.x 发不了语音气泡，这里发的是 mp3 文件——对方点开就能听，内容一样。
        合成失败或发送失败都只记日志，不重试、不打扰对方：文字回复已经发出去了。
        """
        bridge = self._bridge_getter()
        tts = self._tts_getter()
        if not texts or bridge is None or tts is None:
            return
        limit = max(1, int(self.cfg.get("voice.max_per_turn", 1)))
        for text in texts[:limit]:
            path = tts.synth(text)
            if path is None:
                logger.warning("语音合成失败，这条不发了：%s", text[:24])
                continue
            if bridge.send_file(chat_id, str(path)):
                logger.info("已发语音：%s", path.name)
            else:
                logger.error("语音没发出去：%s -> %s", path.name, chat_id)

    # ------------------------------------------------------------------ 内部
    def _send_one(self, bridge: Any, chat_id: str, bubble: str) -> bool:
        """发一条气泡，失败按 2s / 5s 各重试一次。

        发送是整套方案最脆的一环（要驱动微信界面），一次 UIA 抖动就丢一句话。
        实现方案承诺的就是「重试两次后留档放弃」。重试会占住当前会话的锁，
        所以次数不能多——宁可留档让人补，也不能把会话线程堵死。
        """
        for attempt, delay in enumerate(RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            self._respect_cooldown(chat_id)
            if bridge.send_text(chat_id, bubble):
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
        if len(self._last_sent) > COOLDOWN_CAP:
            cutoff = time.time() - 3600
            self._last_sent = {
                key: value for key, value in self._last_sent.items() if value >= cutoff
            }
