# -*- coding: utf-8 -*-
"""工具注册表。

每个工具就是「名字 + 给模型看的说明 + JSON Schema + 一个函数」。
调用一律走 Registry.call()，异常在这里吃掉变成一句错误说明——工具炸了
不能让整条回复挂掉，模型看到「搜索失败」还能换个说法继续聊。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    func: Callable[..., str]

    def spec(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Registry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._cache: Dict[str, str] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> List[str]:
        return list(self._tools)

    def specs(self) -> List[Dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    def call(self, name: str, arguments: str) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"（没有名为 {name} 的工具）"

        args = _parse_args(arguments)
        if args is None:
            logger.warning("工具 %s 参数解析失败：%r", name, (arguments or "")[:120])
            return "（工具参数不是合法 JSON，这次调用作废了，你可以换个写法再试一次）"

        # 同一个工具同样参数问两遍，直接回缓存——模型偶尔会自己循环
        key = "%s|%s" % (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key in self._cache:
            return self._cache[key] + "\n\n（这一轮已经查过一样的内容，结果就是上面这些）"

        try:
            result = tool.func(**args)
        except TypeError as exc:
            logger.warning("工具 %s 参数不匹配：%s", name, exc)
            return f"（工具参数不对：{exc}）"
        except Exception as exc:
            logger.warning("工具 %s 执行失败：%s", name, exc, exc_info=True)
            return f"（工具执行失败：{exc}）"

        text = "（没有结果）" if result is None else str(result)[:8000]
        self._cache[key] = text
        return text


def _parse_args(arguments: str) -> Optional[Dict[str, Any]]:
    """解析工具参数。模型偶尔会吐出半截 JSON（结尾多个冒号之类），先修一遍。"""
    raw = (arguments or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    fixed = re.sub(r"::+\s*(?=[,}\]])", "", raw)      # {"a": 1:: }
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)      # 尾逗号
    fixed = re.sub(r"}\s*$", "}", fixed)
    try:
        value = json.loads(fixed)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return None


ToolSpec = Optional[Tool]
