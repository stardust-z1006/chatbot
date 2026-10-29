# -*- coding: utf-8 -*-
"""这一轮对话可用的工具集。

每次回复都新建一份 Registry：工具闭包里带着「这次跟谁聊、要发哪条语音」，
多会话并发时不会互相串。构造开销可以忽略，换来的是没有共享可变状态。

remember 写进语义记忆库，和自动入库共用一处 —— 模型主动记的一条与自动截取的
一条落在同一个库里，走同一套查重和淘汰，不存在两套记忆各自为政。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional

from . import kb as kb_tools
from . import weather as weather_tools
from . import web as web_tools
from .registry import Registry, Tool
from .history import HistoryReader, make_tool as make_history_tool

if TYPE_CHECKING:  # 只是给类型检查用，避免循环 import
    from ..memory import MemoryStore

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    chat_id: str
    chat_name: str
    is_group: bool = False
    pending_voice: List[str] = field(default_factory=list)
    # 记忆归属（2026-10-27 加）。remember 写进去的条目必须和「自动入库」同源，
    # 否则模型主动记的又变成无主的，就又回到「谁都能读到」的大锅。
    owner: str = ""
    scope: str = "private"


def _make_clock_tool() -> Tool:
    def now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S %A", time.localtime())

    return Tool(
        name="now",
        description="拿当前日期时间。凡是要算「今天/明天/几天后」「最近」的时候先对一下时间。",
        parameters={"type": "object", "properties": {}, "required": []},
        func=now,
    )


def _make_memory_tool(store: "MemoryStore", ctx: TurnContext) -> Tool:
    def remember(text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "（没内容可记）"
        # 走语义库，享受同一套查重：换个说法再说一遍不会存成两条。
        # owner 必须带 —— 不带就成了无主记忆，谁都能检索到。
        if not ctx.owner:
            return "（这会儿不知道在跟谁说话，先不记了）"
        result = store.put(text, owner=ctx.owner, scope=ctx.scope,
                           chat_id=ctx.chat_id if ctx.scope == "group" else "")
        action = result.get("action")
        if action == "updated":
            return "（记下了，跟之前那条是同一件事，已合并）"
        if action in ("duplicate", "duplicated"):
            return "（这条已经记过了）"
        return "（记下了）"

    return Tool(
        name="remember",
        description=(
            "把一个值得长期记住的事实存下来（对方的偏好、称呼、生日、正在做的事、"
            "约定过的事）。只在对方说了明显值得记住的信息时用，别什么都记。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "一句陈述句，第三人称，如「她养了一只叫年年的猫」"},
            },
            "required": ["text"],
        },
        func=remember,
    )


def _make_voice_tool(ctx: TurnContext, cfg: Any) -> Tool:
    limit = int(cfg.get("voice.max_per_turn", 1))
    max_chars = int(cfg.get("voice.max_chars", 60))

    def speak(text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "（没有内容可念）"
        if len(ctx.pending_voice) >= limit:
            return "（这轮已经排了一条语音，别再发了）"
        if len(text) > max_chars:
            return "（太长了，语音只念一两句短的，超过 %d 字改发文字）" % max_chars
        ctx.pending_voice.append(text)
        return "（已排队，文字回复之后会发出去这条语音）"

    return Tool(
        name="speak",
        description=(
            "把一句话用语音发出去（只能发一两句短话）。适合撒娇、道晚安、"
            "不方便打字的长句、或者对方明显想听声音的时候。大部分情况直接文字回复就好，别滥用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要念出来的话，口语、短，不超过 60 字"},
            },
            "required": ["text"],
        },
        func=speak,
    )


def build(
    cfg: Any,
    store: Optional["MemoryStore"],
    ctx: TurnContext,
    reader: Optional[HistoryReader] = None,
    kb: Any = None,
) -> Registry:
    """组装这一轮的工具。任何一项初始化失败都只是少一个工具，不影响开聊。

    store 允许为 None（自检、只列工具清单时用）——remember 只有真被调用才碰它。
    """
    registry = Registry()
    registry.add(_make_clock_tool())

    # 群里不给记忆工具：群里说的话不该进私人记忆库（隐私 + 会把名额占光）
    if store is not None and cfg.get("memory.longterm", True) and not ctx.is_group:
        registry.add(_make_memory_tool(store, ctx))

    if cfg.get("search.enabled", True):
        for tool in web_tools.make_tools(cfg):
            registry.add(tool)
        registry.add(weather_tools.make_tool(cfg))

    if cfg.get("kb.enabled", True) and kb is not None:
        registry.add(kb_tools.make_tool(kb, cfg))

    if reader is not None and cfg.get("history.enabled", True):
        registry.add(make_history_tool(reader, cfg, current_chat=ctx.chat_id))

    if cfg.get("voice.enabled", False) and cfg.get("voice.mode", "tool") == "tool":
        registry.add(_make_voice_tool(ctx, cfg))

    logger.debug("本轮工具：%s", ", ".join(registry.names()) or "（无）")
    return registry
