# -*- coding: utf-8 -*-
"""后台面板的页面。

页面本体在 `static/index.html`（样式和脚本全内嵌，不引框架、不引 CDN、不联网，
断网也照常打开）。2026-10-29 从本文件的 `PAGE = r\"\"\"…\"\"\"` 里搬出来：
1600 行 HTML 塞在 Python 字符串里，编辑器给不了语法检查、也没法按片段 diff，
改一处样式要在几千行字符串里翻。

三件事必须记住（改页面之前先读）：
  1. 面板每 2 秒拉一次状态；**参数输入框不能被刷新冲掉**（用户正在打字）。
  2. 每个分区只在自己被打开时去拉数据（记忆库、资料库、日志量都不小）。
  3. 所有来自后端的文本都要 esc() 一遍再进 innerHTML。

为什么是「起服务时读一次文件」而不是继续内嵌：面板只有一个页面、只在启动时读，
多一次磁盘 IO 换来可编辑、可 diff、可被编辑器检查，这笔交换很划算。
"""
from __future__ import annotations

from pathlib import Path

_PAGE_PATH = Path(__file__).with_name("static") / "index.html"


def load_page() -> str:
    """读页面。文件不见了要立刻报出完整路径，而不是让面板返回一张白页。"""
    try:
        return _PAGE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"面板页面缺失：{_PAGE_PATH}（{exc}）") from exc


# 兼容旧的引用方式（server.py 里 `from .page import PAGE`）。
# 读文件发生在 import 时，和原来把字符串写在模块里对调用方没有区别。
PAGE = load_page()
