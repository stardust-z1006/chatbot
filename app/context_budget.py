# -*- coding: utf-8 -*-
"""闸门：唯一允许往上下文里塞东西的地方。

任何模块（检索、记忆、将来的日报）都得把候选交给这里，由它按预算决定进多少。
超限的回砍顺序：先按分数从低往高丢记忆条数，再丢最老的对话——最近两轮永不砍。
记忆单条的 120 字上限是在拼装时就先截掉的，不是超限了才截。

为什么不设多个入口：一旦有第二个地方能直接拼上下文，预算就不再可信，
迟早会出现「某天上下文爆了但查不出是谁塞的」。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from .llm import estimate_tokens
from .retrieval import Hit

logger = logging.getLogger(__name__)

MEMORY_HEADER = "\n\n【你记得的事】对方以前说过这些，用得着就用，别复述：\n"


@dataclass
class Assembled:
    messages: List[Dict[str, str]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)


class ContextBudget:
    def __init__(
        self,
        context_limit: int = 32000,
        reply_reserve: int = 1200,
        dialog_ratio: float = 0.7,
        memory_ratio: float = 0.3,
        keep_turns: int = 2,
        memory_text_cap: int = 120,
    ) -> None:
        self.context_limit = int(context_limit)
        self.reply_reserve = int(reply_reserve)
        self.dialog_ratio = float(dialog_ratio)
        self.memory_ratio = float(memory_ratio)
        self.keep_turns = max(1, int(keep_turns))
        self.memory_text_cap = int(memory_text_cap)

    # ------------------------------------------------------------------
    def build(
        self,
        system: str,
        turns: Sequence[Dict[str, Any]],
        hits: Sequence[Hit],
        label_speakers: bool = False,
    ) -> Assembled:
        """拼上下文。turns 是时间正序的历史消息，最后一条应是刚收到的这句。

        Args:
            label_speakers: 群聊传 True，给每条 user 消息加上「名字：」前缀。
                私聊不传：只有一个对方，每条都加前缀纯噪音，模型还会学着模仿。
        """
        budget = max(1000, self.context_limit - self.reply_reserve)
        dialog_budget = int(budget * self.dialog_ratio)
        memory_budget = int(budget * self.memory_ratio)

        kept_turns, turn_tokens = self._fill_turns(turns, dialog_budget, label_speakers)
        memory_block, kept_hits, memory_tokens = self._fill_memory(hits, memory_budget)

        system_text = system + memory_block
        total = turn_tokens + memory_tokens + estimate_tokens(system_text)

        # 还是超了：先按分数从低往高丢记忆，再丢最老的对话（最近两轮保住）
        dropped = 0
        while total > budget and kept_hits:
            dropped += 1
            kept_hits.pop()
            memory_block, _, memory_tokens = self._fill_memory(kept_hits, memory_budget)
            system_text = system + memory_block
            total = turn_tokens + memory_tokens + estimate_tokens(system_text)

        trimmed_turns = 0
        while total > budget and len(kept_turns) > self.keep_turns * 2:
            removed = kept_turns.pop(0)
            trimmed_turns += 1
            turn_tokens -= estimate_tokens(removed.get("content", ""))
            total = turn_tokens + memory_tokens + estimate_tokens(system_text)

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_text}]
        messages.extend(kept_turns)

        stats = {
            "tokens": total,
            "budget": budget,
            "memories": len(kept_hits),
            "memories_dropped": dropped,
            "turns": len(kept_turns),
            "turns_trimmed": trimmed_turns,
            "over_limit": total > budget,
        }
        if stats["over_limit"]:
            logger.warning("上下文仍超预算（%d > %d），已尽力回砍", total, budget)
        return Assembled(messages, stats)

    # ------------------------------------------------------------------
    @staticmethod
    def _render(turn: Dict[str, Any], label_speakers: bool) -> Dict[str, str]:
        """把存着的历史消息渲染成真正要发出去的那一条。

        只输出 role / content 两个字段：`_turns` 里为了分人还挂着 name / uid，
        原样塞进请求的话，很多兼容 OpenAI 的接口遇到未知字段会直接 400。

        说话人写进**正文**而不是挂在 name 字段上，是同一个理由的另一半：
        前缀是人眼和模型都读得懂的形式，不依赖接口支不支持这个字段。
        """
        content = str(turn.get("content") or "")
        name = str(turn.get("name") or "").strip()
        if label_speakers and turn.get("role") == "user" and name:
            content = f"{name}：{content}"
        return {"role": str(turn.get("role") or "user"), "content": content}

    def _fill_turns(self, turns: Sequence[Dict[str, Any]], budget: int,
                    label_speakers: bool = False):
        """从最新往回收，收满为止。最近两轮无条件保留（否则对话会断片）。"""
        picked: List[Dict[str, str]] = []
        used = 0
        for turn in reversed(turns):
            msg = self._render(turn, label_speakers)
            cost = estimate_tokens(msg["content"])
            # 先保底 keep_turns 轮，保底之后才看预算
            if used + cost > budget and len(picked) >= self.keep_turns * 2:
                break
            picked.append(msg)
            used += cost
        picked.reverse()
        return picked, used

    def _fill_memory(self, hits: Sequence[Hit], budget: int):
        """按分数从高到低塞，塞不下就停。文本超长的先截断再说，不整条丢。"""
        if not hits:
            return "", [], 0
        lines: List[str] = []
        kept: List[Hit] = []
        used = estimate_tokens(MEMORY_HEADER)
        for hit in hits:
            text = hit.memory.text.strip().replace("\n", " ")
            if len(text) > self.memory_text_cap:
                text = text[: self.memory_text_cap] + "…"
            cost = estimate_tokens(text) + 2
            if used + cost > budget:
                break
            lines.append("· " + text)
            kept.append(hit)
            used += cost
        if not lines:
            return "", [], 0
        return MEMORY_HEADER + "\n".join(lines) + "\n", kept, used
