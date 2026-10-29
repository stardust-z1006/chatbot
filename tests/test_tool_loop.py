# -*- coding: utf-8 -*-
"""回归：工具循环（不联网、不调模型、不碰微信）。

真模型不好伺候：它有时查一次就答，有时查三次，还可能一直想查。所以这里
用一个假 LLM 顶替它，把工具循环的每条分支都走一遍：

  1. 正常一轮：要求调工具 → 结果回填 → 再问 → 出正文
  2. 预算截断：工具结果太长，按剩余 token 砍掉并注明「已截断」
  3. 轮次用尽：一直在调工具，最后一轮必须不带 tools 逼它说人话
  4. 坏输入：未知工具、参数不是 JSON，都要降级成一句话而不是抛异常

跑法：
    .venv\\Scripts\\python.exe tests\\test_tool_loop.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot import Bot  # noqa: E402
from app.panel.state import MonitorState  # noqa: E402
from app.llm import Reply  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.tools.registry import Registry, Tool  # noqa: E402
from _support import FAILS, check  # noqa: E402



class FakeLLM:
    """按剧本回话。剧本用完就回一句固定正文，模拟「终于肯说话了」。"""

    def __init__(self, script: List[Reply], fallback: str = "好，我直接说。") -> None:
        self.script = list(script)
        self.fallback = fallback
        self.calls: List[bool] = []          # 每次调用是否带了 tools
        self.seen: List[List[Dict[str, Any]]] = []

    def chat(self, messages, tools=None, max_tokens=None) -> Reply:
        self.calls.append(bool(tools))
        self.seen.append([dict(m) for m in messages])
        if self.script:
            return self.script.pop(0)
        return Reply(content=self.fallback)


def _bot(cfg: Settings, llm: FakeLLM) -> Bot:
    """不走 Bot.__init__：那里要加载向量模型，这个演示只测工具循环。

    所以直接拿个空壳塞 cfg、假模型和状态收集器——_run_tools 只依赖这三样。
    """
    bot = Bot.__new__(Bot)
    bot.cfg = cfg
    bot.llm = llm  # type: ignore[assignment]
    bot.state = MonitorState()
    return bot


def _registry(echo_len: int = 20) -> Registry:
    registry = Registry()
    registry.add(
        Tool(
            name="now",
            description="报时",
            parameters={"type": "object", "properties": {}, "required": []},
            func=lambda: "2026-10-26 13:20:00 Monday",
        )
    )
    registry.add(
        Tool(
            name="blow",
            description="吐一大段",
            parameters={"type": "object", "properties": {}, "required": []},
            func=lambda: "深" * echo_len,
        )
    )
    return registry


def _call(tool: str, args: str = "{}", cid: str = "c1") -> Reply:
    return Reply(content="", tool_calls=[{"id": cid, "name": tool, "arguments": args}])


# ------------------------------------------------------------------ 用例
def case_normal() -> None:
    print("\n1. 正常一轮：调 now → 回填 → 出正文")
    cfg = Settings({"llm": {"tool_rounds": 4, "tool_budget": 6000}}, ROOT)
    llm = FakeLLM([_call("now"), Reply(content="现在下午一点二十。")])
    messages: List[Dict[str, Any]] = [{"role": "user", "content": "现在几点"}]

    text, meta = _bot(cfg, llm)._run_tools(messages, _registry())

    check("拿到正文", text == "现在下午一点二十。", text)
    check("记下了工具名", meta["tools"] == ["now"], str(meta["tools"]))
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    check("回填了一条 tool 消息", len(tool_msgs) == 1)
    check("tool 消息带 tool_call_id", tool_msgs and tool_msgs[0].get("tool_call_id") == "c1")
    assistant = [m for m in messages if m.get("role") == "assistant"]
    check("assistant 消息带 tool_calls（漏了下一轮就失忆）",
          bool(assistant and assistant[0].get("tool_calls")))
    check("第一轮带了 tools，第二轮也带", llm.calls == [True, True], str(llm.calls))


def case_budget() -> None:
    print("\n2. 预算截断：工具吐 2000 字，预算只给 100 token")
    cfg = Settings({"llm": {"tool_rounds": 4, "tool_budget": 100}}, ROOT)
    llm = FakeLLM([_call("blow"), Reply(content="太长不看。")])
    messages: List[Dict[str, Any]] = [{"role": "user", "content": "来点长的"}]

    text, _ = _bot(cfg, llm)._run_tools(messages, _registry(echo_len=2000))
    tool_msgs = [m for m in messages if m.get("role") == "tool"]

    check("出正文", text == "太长不看。", text)
    check("结果被截断并注明", tool_msgs and "已截断" in tool_msgs[0]["content"])
    check("截断后比原文短很多", tool_msgs and len(tool_msgs[0]["content"]) < 400,
          f"{len(tool_msgs[0]['content']) if tool_msgs else 0} 字")


def case_exhausted() -> None:
    print("\n3. 轮次用尽：一直想调工具，最后一轮必须不传 tools")
    cfg = Settings({"llm": {"tool_rounds": 2, "tool_budget": 6000}}, ROOT)
    llm = FakeLLM([_call("now", cid="a"), _call("now", cid="b", args="{}")],
                  fallback="好了，我直接答。")
    messages: List[Dict[str, Any]] = [{"role": "user", "content": "几点"}]

    text, meta = _bot(cfg, llm)._run_tools(messages, _registry())

    check("仍然拿到了正文", text == "好了，我直接答。", text)
    check("调了 2 轮（等于 tool_rounds）", meta["rounds"] == 2, str(meta["rounds"]))
    check("收尾那一次没传 tools", llm.calls == [True, True, False], str(llm.calls))
    check("轮次用完还追了一条收尾提示",
          any("别再调工具" in str(m.get("content")) for m in messages))


def case_bad_input() -> None:
    print("\n4. 坏输入：未知工具 / 参数不是 JSON")
    registry = _registry()
    unknown = registry.call("nope", "{}")
    broken = registry.call("now", "{不是 json")
    check("未知工具降级成说明", "没有名为 nope 的工具" in unknown, unknown[:30])
    check("坏 JSON 降级成说明", "参数不是合法 JSON" in broken, broken[:30])


def case_empty_answer() -> None:
    print("\n5. 只吐工具调用不给正文：兜底再问一次")
    cfg = Settings({"llm": {"tool_rounds": 4, "tool_budget": 6000}}, ROOT)
    llm = FakeLLM([_call("now"), Reply(content="")], fallback="一点二十。")
    messages: List[Dict[str, Any]] = [{"role": "user", "content": "几点"}]

    text, _ = _bot(cfg, llm)._run_tools(messages, _registry())
    check("空正文被兜住", text == "一点二十。", text)
    check("兜底那次没传 tools", llm.calls[-1] is False, str(llm.calls))


def case_ceiling() -> None:
    print("\n6. 上下文见顶：不再调工具，逼模型直接答（防接口报上下文超长）")
    cfg = Settings({"llm": {"tool_rounds": 5, "tool_budget": 6000}}, ROOT)
    # 不给剧本：见顶之后那次调用应该直接出正文，而且不带 tools
    llm = FakeLLM([], fallback="查不动了，我直接说。")
    # 拼装结果本身就超过 ceiling：第一轮就该见顶，一次工具都不许调
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": "帮我查一下" * 40}]

    text, meta = _bot(cfg, llm)._run_tools(messages, _registry(), ceiling=20)

    check("还是拿到了正文", text == "查不动了，我直接说。", text)
    check("工具一次都没调成", meta["tools"] == [], str(meta["tools"]))
    check("收尾提示写进去了",
          any("上下文快满了" in str(m.get("content")) for m in messages))
    check("一次都没带 tools", llm.calls == [False], str(llm.calls))


def main() -> int:
    print("=" * 52)
    print(" 工具循环回归（假模型，不联网）")
    print("=" * 52)
    case_normal()
    case_budget()
    case_exhausted()
    case_bad_input()
    case_empty_answer()
    case_ceiling()
    print()
    if FAILS:
        print(f"有 {len(FAILS)} 项没过：" + "、".join(FAILS))
        return 1
    print("全部通过。工具循环可以接真模型了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
