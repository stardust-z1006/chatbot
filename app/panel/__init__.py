# -*- coding: utf-8 -*-
"""后台监控：运行状态、最近消息、日志环、参数热改。

拆成四块，各管一件事：
  - state.py   收集运行数据（计数、环、日志），只依赖标准库
  - schema.py  参数清单（类型/范围/分组/要不要重启），只依赖 Settings
  - server.py  HTTP 接口 + 起停，这里才 import fastapi
  - page.py    单页前端（内嵌样式脚本，不联网）

这个 __init__ 故意不 import 任何子模块：机器人主流程只用到 state，
不该因为它顺带把 fastapi 也拽进内存（命令行 ask 会慢一截）。
用的时候按需 from .panel.state import MonitorState 这样拿。
"""
