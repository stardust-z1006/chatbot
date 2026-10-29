# -*- coding: utf-8 -*-
"""查重灰区的裁判。

为什么要它：向量相似度落在 0.80~0.96 这一段时，光看数字分不清
「换了个说法」和「说的是另一件事」。这一段的量不大（大部分消息要么明显新，
要么明显重复），所以值得花一次模型调用。

没配密钥、或者调用失败时返回 None，让上层按「先存下、打标记」处理——
宁可留一条待合并的，也不能因为裁判缺席就丢用户的记忆。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

SYSTEM = (
    "你是记忆查重器。只判断两句话是否指向同一件事，按格式输出一个字。"
)

PROMPT = (
    "【规则】\n"
    "1. 措辞不同、详略不同、角度不同，但说的是同一件事实或同一个偏好 -> 是\n"
    "2. 只是话题相关，实际是两件事 -> 否\n"
    "3. 观点相反（喜欢/讨厌）-> 否\n\n"
    "【示例】\n"
    "输入A：我常喝冰美式\n"
    "输入B：我喝咖啡只喝美式，不加糖\n"
    "输出：是\n\n"
    "【待判断】\n"
    "输入A：{old}\n"
    "输入B：{new}\n"
    "输出："
)

SAME_WORDS = ("是", "同一", "相同", "yes", "true", "1")


class DedupJudge:
    """用一次小调用判断「同一件事」。调用失败一律返回 None（不确定）。"""

    def __init__(self, cfg: Any, timeout: float = 12.0) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self._client = None
        self._broken = False

    def _get_client(self):
        if self._client is not None:
            return self._client
        key = getattr(self.cfg, "api_key", "") or ""
        if not key:
            self._broken = True
            return None
        try:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=key,
                base_url=getattr(self.cfg, "base_url", "https://api.deepseek.com"),
                timeout=self.timeout,
                max_retries=0,
            )
            return self._client
        except Exception as exc:
            logger.warning("裁判客户端初始化失败：%s", exc)
            self._broken = True
            return None

    def __call__(self, new_text: str, old_text: str) -> Optional[bool]:
        """True = 同一件事；False = 不同；None = 不确定（调用失败/没配）。"""
        if self._broken:
            return None
        client = self._get_client()
        if client is None:
            return None
        try:
            resp = client.chat.completions.create(
                model=self.cfg.get("llm.model", "deepseek-v4-flash"),
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": PROMPT.format(old=old_text, new=new_text)},
                ],
                temperature=0,
                # 它是会先「想」再答的模型，给 4 个 token 会直接被截成空字符串。
                # 实测 256 才够它想完再吐一个字。
                max_tokens=256,
            )
            answer = (resp.choices[0].message.content or "").strip().lower()
        except Exception as exc:
            # 只有「钥匙不对/没权限」才永久停用；超时、限流这类是一时的，
            # 下次该继续问——不然一次网络抖动就把查重裁判永久关掉了，
            # 灰区记忆从此每条都存成两条，越攒越脏。
            if _is_fatal(exc):
                self._broken = True
                logger.warning("裁判鉴权失败，本轮进程内停用：%s", exc)
            else:
                logger.info("裁判这次没问成，下条继续试：%s", exc)
            return None
        if not answer:
            # 空回复 = 没答上来，不能当成「否」，否则会把该合并的记忆拆成两条
            logger.warning("裁判返回空内容，按不确定处理")
            return None
        return answer.startswith(SAME_WORDS) or answer in SAME_WORDS

    @property
    def available(self) -> bool:
        return not self._broken


def _is_fatal(exc: Exception) -> bool:
    """这个错误是不是「再试也没用」。

    401/403 是钥匙不对；余额不足（402）也算，重试只会一直失败。
    超时、连接重置、429 限流都是临时的，不该把裁判永久关掉。
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and status in (401, 402, 403):
        return True
    text = str(exc).lower()
    return any(mark in text for mark in (
        "invalid api key", "incorrect api key", "unauthorized", "authentication",
        "insufficient balance", "insufficient_quota",
    ))
