# -*- coding: utf-8 -*-
"""tests 共用脚手架：断言计数 + 分组标题。

十个回归脚本原来各抄了一份 ``check()`` / ``line()``（其中 8 份逐字相同），
改一处要改八处，还容易漏。现在只留这一份。

设计上有意不抛异常：一个用例挂了也要把后面的全跑完，一次看到所有失败项，
比 pytest 那种「第一处断言就中断」更适合这种手工核对脚本。

用法::

    from _support import FAILS, check, line, total
    check("存进去能捞出来", hits[0].text == "我喝美式", str(hits))
    line("3. 淘汰")
    ...
    print(f"断言 {total()} 项，失败 {len(FAILS)} 项")
"""
from __future__ import annotations

from typing import List

# 失败项名字。测试脚本直接读它汇总，所以必须是模块级共享的同一份列表。
FAILS: List[str] = []

_TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """记一条断言。不中断执行。"""
    global _TOTAL
    _TOTAL += 1
    print(("  [通过] " if ok else "  [失败] ") + name + ("　" + detail if detail else ""))
    if not ok:
        FAILS.append(name)


def line(title: str = "") -> None:
    """打一条分隔线，给一组断言起个标题。"""
    print()
    print("-" * 60)
    if title:
        print(title)
        print("-" * 60)


def total() -> int:
    """到目前为止记了几条断言（打印汇总用）。"""
    return _TOTAL
