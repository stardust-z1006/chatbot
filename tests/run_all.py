# -*- coding: utf-8 -*-
"""一次跑完所有回归脚本。

    .venv\\Scripts\\python.exe tests\\run_all.py          # 全跑
    .venv\\Scripts\\python.exe tests\\run_all.py memory   # 只跑名字含 memory 的

十一个脚本互相独立、各自起临时目录，任何两个之间没有顺序依赖，
所以这里按「由便宜到贵」排：先跑几秒能出结果的，挂得早也好早发现。
每个子进程单独跑，一个脚本崩了不影响其余的，最后汇总一张表。

不对接 pytest：这些脚本是「打印一份可读报告 + 断言计数」的手工核对工具，
需要看见中间过程。pytest 会吃掉输出，反而不好用。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# 由便宜到贵，名字 -> 一句话说明（说明用于汇总表和失败时指路）
SUITES = [
    ("test_context_budget.py", "闸门预算回砍、回复切分、重复消息"),
    ("test_memory.py", "记忆写入、检索门槛、查重合并、容量淘汰"),
    ("test_identity_isolation.py", "别人的记忆一条都捞不到"),
    ("test_message_content.py", "XML 折叠 / 非文本占位：不再污染上下文、不再静默丢弃"),
    ("test_concurrency.py", "并发：同会话串行 / 跨会话并行 / 指纹恰好一次"),
    ("test_tool_loop.py", "工具循环：正常 / 预算截断 / 轮次用尽 / 坏输入"),
    ("test_group_engage.py", "群聊接话打分：被叫到就回、不插嘴"),
    ("test_group_log.py", "关键用户群消息记录（只追加流水）"),
    ("test_panel.py", "后台面板：接口、改参数、恢复默认、重建索引"),
    ("test_send_guard.py", "发送护栏：同一条回复不会被发两遍"),
    ("test_integration.py", "端到端：真 Bot + 假微信 + 假模型"),
]


def main(argv: list[str]) -> int:
    pick = argv[1].lower() if len(argv) > 1 else ""
    suites = [s for s in SUITES if pick in s[0].lower()] if pick else SUITES
    if not suites:
        print(f"没有名字含「{pick}」的脚本。可选：")
        for name, desc in SUITES:
            print(f"  {name:32s} {desc}")
        return 2

    results = []
    for name, desc in suites:
        path = HERE / name
        print()
        print("=" * 64)
        print(f"▶ {name}   {desc}")
        print("=" * 64)
        started = time.time()
        proc = subprocess.run([sys.executable, str(path)], cwd=str(ROOT))
        cost = time.time() - started
        results.append((name, proc.returncode, cost))

    print()
    print("=" * 64)
    print(" 汇总")
    print("=" * 64)
    width = max(len(n) for n, _, _ in results)
    for name, code, cost in results:
        mark = "通过" if code == 0 else f"失败(rc={code})"
        print(f"  {name:{width}s}  {mark:12s} {cost:6.1f}s")

    bad = [n for n, c, _ in results if c != 0]
    print()
    if bad:
        print(f"  {len(bad)} 个脚本没过：" + "、".join(bad))
        return 1
    print(f"  {len(results)} 个脚本全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
