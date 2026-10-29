# -*- coding: utf-8 -*-
"""日志：控制台 + 轮转文件，全项目只有一套格式。

两个 Windows 上的坑：
  1. 控制台默认 GBK，中文日志会抛 UnicodeEncodeError 把线程打死 → 强制 UTF-8。
  2. wechatauto 导入时会 logging.getLogger().handlers.clear()，把我们装好的
     handler 全清掉。所以导入它之后必须调一次 ensure() 把 handler 补回来。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# 记住上一次的配置，供 ensure() 原地重装
_STATE: dict = {"level": "INFO", "file": None, "wechat_debug": False}

# 「重装 handler 时也要带回来」的处理器。面板的日志环挂这里——
# wechatauto 一导入就会 clear 掉 root 上所有 handler，不重新挂回去，
# 面板的日志环从那一刻起就再也收不到东西（踩过）。
_EXTRA: list = []

_NOISY = ("httpx", "httpx2", "httpcore", "httpcore2", "openai",
          "urllib3", "websockets", "asyncio")


def register_extra(handler: logging.Handler) -> None:
    """挂一个额外 handler，并保证 setup()/ensure() 重装时也带上它。"""
    if handler not in _EXTRA:
        _EXTRA.append(handler)
    root = logging.getLogger()
    if handler not in root.handlers:
        handler._bot_handler = True  # type: ignore[attr-defined]  # 别被 reassert() 踢掉
        root.addHandler(handler)


def unregister_extra(handler: logging.Handler) -> None:
    """摘掉额外 handler（进程退出、面板关掉时用）。"""
    if handler in _EXTRA:
        _EXTRA.remove(handler)
    try:
        logging.getLogger().removeHandler(handler)
    except Exception:  # noqa: BLE001
        pass


def setup(
    level: str = "INFO",
    log_file: Optional[Path] = None,
    wechat_debug: bool = False,
) -> logging.Logger:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    _STATE["level"] = str(level).upper()
    _STATE["file"] = log_file
    _STATE["wechat_debug"] = bool(wechat_debug)

    root = logging.getLogger()
    root.setLevel(getattr(logging, _STATE["level"], logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console._bot_handler = True  # type: ignore[attr-defined]
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            log_file, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(fmt)
        rotating._bot_handler = True  # type: ignore[attr-defined]
        root.addHandler(rotating)

    # 这些库的 DEBUG 太吵
    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 微信接入库的日志走同一个文件，不再单独生成 wechatauto_logs/
    logging.getLogger("wechatauto").setLevel(
        logging.DEBUG if _STATE["wechat_debug"] else logging.INFO
    )

    # 重装时把额外 handler（面板日志环）补回来
    for extra in _EXTRA:
        if extra not in root.handlers:
            extra._bot_handler = True  # type: ignore[attr-defined]
            root.addHandler(extra)

    return logging.getLogger("bot")


def installed() -> bool:
    return any(getattr(h, "_bot_handler", False) for h in logging.getLogger().handlers)


def ensure() -> None:
    """handler 被第三方库清掉后补回来。导入 wechatauto 之后必须调。"""
    if not installed():
        setup(_STATE["level"], _STATE["file"], _STATE["wechat_debug"])


def reassert() -> None:
    """踢掉第三方库偷偷加到 root 上的 handler。

    openai / httpx2 在 import 时会调 logging.basicConfig()，给 root 塞一个
    自己的格式化器，于是日志变成两种风格混排。这里只留我们自己标记过的。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if not getattr(handler, "_bot_handler", False):
            root.removeHandler(handler)
