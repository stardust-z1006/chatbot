# -*- coding: utf-8 -*-
"""向量化：本地小模型，坏了就降级。

为什么走本地：不用注册第三方、记忆原文不出本机、断网也能跑。
代价：首次要下模型（约 45 秒）；短句区分度一般，这是 small 档的极限。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# 这两个开关必须一起设，否则模型下载在 Windows 上必定失败：
#   1. HF 缓存默认用符号链接，普通权限建不了 -> 报 NO_SUCHFILE
#   2. 直连 huggingface 的 CDN 会 read timeout
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


class Embedder:
    """懒加载向量器。构造时不碰模型，第一次真正要用才加载。"""

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model = None
        self._broken = False

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """模型加载成功过就为真。坏了之后不再反复重试，避免每条消息都卡一次。"""
        return not self._broken

    def _load(self):
        if self._model is not None:
            return self._model
        if self._broken:
            return None
        try:
            from fastembed import TextEmbedding

            kwargs = {"model_name": self.model_name}
            if self.cache_dir is not None:
                kwargs["cache_dir"] = str(self.cache_dir)
            self._model = TextEmbedding(**kwargs)
            logger.info("向量模型就绪：%s", self.model_name)
            return self._model
        except Exception as exc:  # noqa: BLE001
            # 一次失败就永久降级：让它走关键词兜底，总比每条消息都卡在下载上强
            self._broken = True
            logger.warning("向量模型不可用，退关键词检索：%s", exc)
            return None

    # ------------------------------------------------------------------
    def encode(self, texts: Sequence[str]) -> List[Optional[List[float]]]:
        """批量编码。单条失败返回 None，绝不把异常抛给上层。"""
        items = [str(t or "") for t in texts]
        if not items:
            return []
        model = self._load()
        if model is None:
            return [None] * len(items)
        out: List[Optional[List[float]]] = []
        try:
            for vec in model.embed(items):
                out.append(_normalize([float(x) for x in vec]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("向量编码失败：%s", exc)
            return [None] * len(items)
        return out

    def encode_one(self, text: str) -> Optional[List[float]]:
        return self.encode([text])[0]


def _normalize(vec: List[float]) -> List[float]:
    """归一化存起来，检索时就只剩点积，省掉每次算模长。"""
    total = sum(x * x for x in vec) ** 0.5
    if total <= 1e-12:
        return vec
    return [x / total for x in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """两个已归一化向量的相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b, strict=True)))
