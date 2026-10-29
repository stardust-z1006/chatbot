# -*- coding: utf-8 -*-
"""模型调用。

和普通 OpenAI 兼容客户端不同的三点，都是被 bug 教出来的：
  1. deepseek-v4-flash 会先「想」再答，思考 token 也算在 max_tokens 里。
     给小了会拿到空字符串（实测 4 个 token 就是空），所以空回复要放大重试。
  2. 它偶尔会把内部标记漏到正文里（像是工具调用的残渣），发到微信之前必须清掉。
  3. 决定调工具时 content 是空的 —— 这是正常的，不是「没答上来」。
     只有既没有 tool_calls 又没有正文才算空回复，否则会把「它想查个天气」
     误判成失败，白白放大预算重试两遍。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 模型偶尔吐出来的内部标记，不能让它进微信
_JUNK = re.compile(r"<[｜|]{1,2}[^>]{0,40}[｜|]{1,2}>|<\|[^|]{0,40}\|>")


class LLMError(RuntimeError):
    pass


@dataclass
class Reply:
    """一次调用的结果：可能带正文，可能带工具调用，也可能两者都有。"""

    content: str = ""
    tool_calls: List[Dict[str, str]] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def as_msg_dict(self) -> Dict[str, Any]:
        """拼回 assistant 消息塞进历史。带工具调用时必须带上 tool_calls，
        否则下一轮模型会不知道自己刚才要求过什么。"""
        msg: Dict[str, Any] = {"role": "assistant"}
        if self.content:
            msg["content"] = self.content
        else:
            msg["content"] = ""
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for c in self.tool_calls
            ]
        return msg


class LLM:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.85,
        max_tokens: int = 1200,
        timeout: float = 45.0,
        retries: int = 2,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise LLMError("缺少 API Key：在 .env 里填 DEEPSEEK_API_KEY")
        try:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,  # 重试自己管，才能控制「放大预算」这一步
            )
        except Exception as exc:
            raise LLMError(f"客户端初始化失败：{exc}") from exc
        return self._client

    # ------------------------------------------------------------------
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
    ) -> "Reply":
        """发一轮对话，返回 Reply（可能带工具调用）。失败抛 LLMError。"""
        client = self._get_client()
        budget = int(max_tokens or self.max_tokens)
        last_exc: Optional[Exception] = None

        for attempt in range(self.retries + 1):
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": budget,
            }
            if tools:
                kwargs["tools"] = tools
            try:
                resp = client.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                message = choice.message
                calls = _extract_calls(message)
                text = _clean(message.content or "")

                # 想调工具时正文是空的，这是它表达意图的方式，不是失败
                if calls:
                    return Reply(content=text, tool_calls=calls)
                if text:
                    return Reply(content=text)

                # 什么都没有 = 思考把预算吃完了。放大一倍再来，别把空话发给用户。
                if getattr(choice, "finish_reason", "") == "length" and attempt < self.retries:
                    budget = min(budget * 2, 4000)
                    logger.warning("模型返回空内容（预算被思考吃掉），放大到 %d 重试", budget)
                    continue
                raise LLMError("模型返回空内容")
            except LLMError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        raise LLMError(f"调用失败：{last_exc}")


def _extract_calls(message: Any) -> List[Dict[str, str]]:
    """从响应里取出工具调用。不同兼容层的属性细节不一样，一律 getattr 兜着。"""
    out: List[Dict[str, str]] = []
    for call in getattr(message, "tool_calls", None) or []:
        fn = getattr(call, "function", None)
        if fn is None:
            continue
        name = str(getattr(fn, "name", "") or "").strip()
        if not name:
            continue
        out.append(
            {
                "id": str(getattr(call, "id", "") or ""),
                "name": name,
                "arguments": str(getattr(fn, "arguments", "") or "{}"),
            }
        )
    return out


def _clean(text: str) -> str:
    text = (text or "").strip()
    if "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    text = _JUNK.sub("", text)
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").strip()
    return text.strip()


def estimate_tokens(text: str) -> int:
    """估算 token。中文按 1 字 ≈ 0.7 token，英文按 4 字符 ≈ 1 token。

    不引 tiktoken：为了一个估算多一个几十 MB 的依赖不值得，
    留 20% 余量比精确计算更实用。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.7 + other / 3.5) + 1


def build_llm(cfg: Any) -> LLM:
    return LLM(
        api_key=getattr(cfg, "api_key", ""),
        base_url=getattr(cfg, "base_url", "https://api.deepseek.com"),
        model=cfg.get("llm.model", "deepseek-v4-flash"),
        temperature=float(cfg.get("llm.temperature", 0.85)),
        max_tokens=int(cfg.get("llm.max_tokens", 1200)),
        timeout=float(cfg.get("llm.timeout", 45)),
        retries=int(cfg.get("llm.retries", 2)),
    )
