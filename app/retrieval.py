# -*- coding: utf-8 -*-
"""检索。

V1 两项打分：语义相似度 0.7 + 重要度 0.3。
主文档的四合一（还要加未解决、唤醒度）留到 V3 —— 四个权重就是四个要调的参数，
第一版先不要给自己造调参黑洞。

门槛是硬的：分数不够就不进上下文，**不凑数**。宁可这次什么也想不起来，
也不要塞三条不相关的记忆进去污染回复。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from .embeddings import Embedder, cosine
from .memory import Memory, MemoryStore, _text_overlap

logger = logging.getLogger(__name__)

# 语义相似度的最低线：再低就不是「相关」了，是「蹭上了」。
SIM_FLOOR = 0.42
# 字面命中时给的下限分：宁可高估，也别让「老张」这种专名漏掉。
LITERAL_BOOST = 0.72


@dataclass
class Hit:
    memory: Memory
    similarity: float
    score: float

    def brief(self) -> str:
        return f"{self.memory.text[:24]}（分 {self.score:.2f} / 相似 {self.similarity:.2f}）"


class MemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        embedder: Optional[Embedder] = None,
        weight_similarity: float = 0.7,
        weight_importance: float = 0.3,
        top_k: int = 6,
        min_score: float = 0.55,
    ) -> None:
        self.store = store
        self.embedder = embedder or store.embedder
        self.w_sim = float(weight_similarity)
        self.w_imp = float(weight_importance)
        self.top_k = int(top_k)
        self.min_score = float(min_score)

    # ------------------------------------------------------------------
    def search(self, query: str, top_k: Optional[int] = None, mark_used: bool = False,
               owner: Optional[str] = None, scope: Optional[str] = None,
               chat_id: Optional[str] = None,
               min_score: Optional[float] = None) -> List[Hit]:
        """检索并排序。任何异常都当作「没检索到」，绝不让回复流程挂掉。

        Args:
            owner: 只在这个归属里找（`u:<wxid>`）。**这是「不同人记忆互不干扰」的落点**，
                不传 = 不过滤（命令行/自检用），传了就只能捞到自己的。
            scope: private / group；群聊必须传 group，否则会把私聊记忆捞进群里。
            chat_id: 群 id，负责跨群隔离。
            min_score: 单次覆盖分数门槛（owner 能看得更低）。None = 用实例默认值，
                这样老调用方一个参数都不用改。

        归属类参数都是 None 时行为和加归属之前**完全一样**，老调用方不受影响。
        """
        query = (query or "").strip()
        if not query:
            return []
        try:
            return self._search(query, int(top_k or self.top_k), mark_used,
                                owner, scope, chat_id, min_score)
        except Exception as exc:
            logger.warning("检索失败，按没检索到处理：%s", exc)
            return []

    def _search(self, query: str, top_k: int, mark_used: bool,
                owner: Optional[str] = None, scope: Optional[str] = None,
                chat_id: Optional[str] = None,
                min_score: Optional[float] = None) -> List[Hit]:
        qvec = self.embedder.encode_one(query)
        hits: List[Hit] = []
        # 每次检索都能单独压门槛：owner 看得更低（0.45），别人用实例默认值。
        floor = self.min_score if min_score is None else float(min_score)

        for mem in self.store.active(owner=owner, scope=scope, chat_id=chat_id):
            sim = self._similarity(query, qvec, mem)
            if sim < SIM_FLOOR:
                continue
            imp = (max(1, min(5, mem.importance)) - 1) / 4.0
            score = self.w_sim * sim + self.w_imp * imp
            if score < floor:
                continue
            hits.append(Hit(mem, sim, score))

        hits.sort(key=lambda h: (h.score, h.memory.last_used_at), reverse=True)
        hits = hits[:top_k]

        if mark_used:
            for hit in hits:
                self.store.touch(hit.memory.id)
        return hits

    def _similarity(self, query: str, qvec: Optional[List[float]], mem: Memory) -> float:
        """向量优先；任一方没有向量就退字面重合；字面命中的再抬一道底。

        同一条记忆可能有两种说法（换过说法），两边都算，取高的——
        否则「我平时就喝美式」会把「美式咖啡」这个词一起丢掉。
        """
        sim = 0.0
        if qvec is not None and mem.vector is not None:
            sim = cosine(qvec, mem.vector)
        else:
            sim = _text_overlap(query, mem.text) * 0.9

        if qvec is not None and mem.alias_vector is not None:
            sim = max(sim, cosine(qvec, mem.alias_vector))

        # 专名、数字这类字面命中，向量常常捞不准（「老张」和「约了谁」不相似）
        if len(query) >= 2 and (query in mem.text or (mem.alias_text and query in mem.alias_text)):
            sim = max(sim, LITERAL_BOOST)
        return sim
