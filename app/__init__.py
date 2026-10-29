# -*- coding: utf-8 -*-
"""包入口。

这里只做一件事：把 vendor/ 挂到 sys.path 上，好让 Python 3.13 也能拿到
winsdk 的兼容垫片（wechatauto 的 OCR 路径要用）。放在包初始化里，是为了保证
任何 ``import app.xxx`` 都先于 ``import wechatauto`` 生效。
"""
from __future__ import annotations

import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
