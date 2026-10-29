# -*- coding: utf-8 -*-
"""入口。

    .venv\\Scripts\\python.exe run.py                 # 不带参数：交互菜单
    .venv\\Scripts\\python.exe run.py run             # 接微信，正式跑（顺带起后台面板）
    .venv\\Scripts\\python.exe run.py selftest        # 验微信发送通道（发一条到文件传输助手）
    .venv\\Scripts\\python.exe run.py doctor          # 环境自检
    .venv\\Scripts\\python.exe run.py index           # 扫 knowledge/ 建资料索引
    .venv\\Scripts\\python.exe run.py ask 推荐个咖啡   # 不接微信，直接测整条链路
    .venv\\Scripts\\python.exe run.py web             # 只开后台面板，不接微信
    .venv\\Scripts\\python.exe run.py open            # 浏览器打开后台面板

回归测试在 tests/ 目录：.venv\\Scripts\\python.exe tests\\run_all.py（九个脚本全跑）。
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import settings  # noqa: E402  （sys.path 先补好，import 必须在后面）
from app.logging_setup import setup as setup_log  # noqa: E402

LOCK_PORT = 47653


def _acquire_lock():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def cmd_ask(cfg, text: str) -> int:
    from app.bot import Bot

    bot = Bot(cfg)
    print(f"人设：{bot.persona.splitlines()[0][:40]}")
    print(f"记忆库：{bot.store.stats()}")
    result = bot.handle("cli", text)

    print()
    print(f"标出：{result['types']}")
    if result["memories"]:
        print("捞到：")
        for line in result["memories"]:
            print("   -", line)
    else:
        print("捞到：无（门槛拦掉了，不凑数）")
    stats = result["stats"]
    print(f"上下文：{stats['tokens']}/{stats['budget']} token，"
          f"{stats['turns']} 条对话 + {stats['memories']} 条记忆")
    if result["tools"]:
        print("工具：" + " → ".join(result["tools"]))
    else:
        print("工具：没用（模型判断不需要）")
    print()
    print("-" * 52)
    print(result["reply"])
    print("-" * 52)
    print(f"耗时 {result['elapsed']:.1f}s")
    bot.store.snapshot()
    return 0


def cmd_index(cfg, rebuild: bool = False) -> int:
    """扫 kb.dirs 建索引。增量：文件没改就跳过。"""
    from app.kb_store import KbStore
    from app.tools.kb import build_index

    dirs = cfg.get("kb.dirs", ["knowledge"]) or []
    for raw in dirs:
        path = cfg.rel(raw) if not Path(raw).is_absolute() else Path(raw)
        if not path.exists():
            print(f"  提示：{path} 不存在，先建目录再把资料放进去")

    kb = KbStore(cfg.data_dir / "kb.sqlite3")
    files, chunks = build_index(kb, cfg, force=rebuild)
    stats = kb.stats()
    print()
    print(f"本次更新 {files} 篇 / 新增 {chunks} 块")
    print(f"索引现状：{stats['docs']} 篇，{stats['chars']} 字，{stats['chunks']} 块")
    if stats["docs"] == 0:
        print("还是空的：资料放 knowledge/ 之后再跑一次（支持 md / txt / pdf / docx）")
    return 0


def cmd_selftest(cfg) -> int:
    """验发送通道（原「发送自检.py」，并进来的）。

    发送是整套方案里最脆的一环（靠界面自动化），排障第一步就是确认
    「真的能发出一条消息」。会在「文件传输助手」里留一条【自检】。
    """
    print("=" * 52)
    print(" deepChat v1 · 发送自检")
    print("=" * 52)

    from app.wechat.bridge import Bridge

    bridge = Bridge(cfg)

    # 能读到自己的账号，说明本地消息库这条线是通的
    info = bridge.self_info()
    if not info.get("username"):
        print("  [失败] 读不到微信账号 —— 微信 PC 端是不是没登录？")
        return 1
    print(f"  [通过] 读到账号：{info.get('nickname')}（{info.get('username')}）")

    report = bridge.selftest("文件传输助手")
    for item in report["steps"]:
        mark = "通过" if item["ok"] else "失败"
        print(f"  [{mark}] {item['name']}：{item['detail']}")

    print()
    if report["ok"]:
        print("结论：发送通道正常。")
        print("     去微信「文件传输助手」看，应该有一条【自检】。")
        return 0

    print("结论：还没通。")
    print("     按上面第一个「失败」那行处理，做完再跑一次。")
    return 1


def cmd_doctor(cfg) -> int:
    print("=" * 52)
    print(" 环境自检")
    print("=" * 52)
    ok = True

    # 向量
    from app.embeddings import Embedder, cosine

    embedder = Embedder()
    vec = embedder.encode_one("我喜欢喝美式")
    if vec:
        print(f"  [通过] 向量模型（{len(vec)} 维）")
        sim = cosine(vec, embedder.encode_one("我平时喝美式") or [])
        print(f"         近义句相似度 {sim:.2f}（低于 0.4 说明模型没生效）")
    else:
        ok = False
        print("  [失败] 向量模型不可用 —— 检索会退化成关键词")

    # 模型
    from app.llm import LLMError, build_llm

    llm = build_llm(cfg)
    try:
        answer = llm.chat([{"role": "user", "content": "只回两个字：在的"}], max_tokens=256)
        print(f"  [通过] 模型 {cfg.get('llm.model')} 回答：{answer.content[:20]}")
    except LLMError as exc:
        ok = False
        print(f"  [失败] 模型调用：{exc}")

    # 资料库（打不开只是少一个工具，所以只提示不判失败）
    kb = None
    if cfg.get("kb.enabled", True):
        try:
            from app.kb_store import KbStore

            kb = KbStore(cfg.data_dir / "kb.sqlite3")
            stat_kb = kb.stats()
            tail = "（空库，放资料到 knowledge/ 后跑 run.py index）" if stat_kb["docs"] == 0 else ""
            print(f"  [通过] 资料库：{stat_kb['docs']} 篇 / {stat_kb['chunks']} 块{tail}")
        except Exception as exc:
            print(f"  [跳过] 资料库打不开（kb_search 不挂）：{exc}")

    # 工具清单。复用刚加载的向量模型，别再加载一遍
    from app.memory import MemoryStore
    from app.tools import TurnContext
    from app.tools import build as build_tools

    try:
        store = MemoryStore(cfg.data_dir / "memory", embedder=embedder)
        names = build_tools(
            cfg, store, TurnContext(chat_id="doctor", chat_name="doctor"), kb=kb
        ).names()
        print(f"  [通过] 工具 {len(names)} 个：{', '.join(names)}")
        if "chat_history" not in names:
            print("         聊天记录工具没挂（命令行模式没有微信接入层，正式跑才有）")
    except Exception as exc:
        ok = False
        print(f"  [失败] 工具组装：{exc}")

    # 微信
    bridge = None
    try:
        from app.wechat.bridge import Bridge

        bridge = Bridge(cfg)
        info = bridge.self_info()
        print(f"  [通过] 微信账号：{info.get('nickname')}（{info.get('username')}）")
    except Exception as exc:
        ok = False
        print(f"  [失败] 微信读不到：{exc}")

    # 微信窗口状态。最小化时 UIA 敲不进输入框，消息会静默发不出去，
    # 这是最难查的一类故障——日志里只有一句「内容未落进输入框」，所以自检直接报出来
    if bridge is not None:
        try:
            bridge.ensure_ready()
            state = bridge.window_state()
            label = {"minimized": "最小化（发不出去）", "hidden": "不可见",
                     "normal": "正常", "unknown": "未知"}.get(state.get("state"), "未知")
            if state.get("state") == "minimized":
                print(f"  [警告] 微信窗口{label}：把微信开出来再跑，"
                      f"否则回复生成了也发不出去")
            else:
                print(f"  [通过] 微信窗口：{label}")
        except Exception as exc:
            print(f"  [跳过] 窗口状态读不到：{exc}")

    print()
    print("结论：" + ("可以跑 run.py run" if ok else "先修上面 [失败] 的"))
    return 0 if ok else 1


def _panel_address(cfg) -> str:
    host = str(cfg.get("monitor.host", "127.0.0.1"))
    port = int(cfg.get("monitor.port", 8787))
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{shown}:{port}"


def _start_panel(cfg, bot, state):
    """起后台面板。起不来只提示，机器人照常跑（面板是附加物，不是前置条件）。"""
    from app.panel import server as monitor

    if not bool(cfg.get("monitor.enabled", True)):
        print(" 后台面板：已关闭（config 里 monitor.enabled = false）")
        return None
    app = monitor.create_app(cfg, bot, state)
    server = monitor.start_in_thread(
        app, str(cfg.get("monitor.host", "127.0.0.1")), int(cfg.get("monitor.port", 8787))
    )
    if server is None:
        print(f" 后台面板：起不来（{_panel_address(cfg)} 可能被占用），机器人不受影响")
        return None
    print(f" 后台面板：{_panel_address(cfg)}")
    return server


def cmd_run(cfg) -> int:
    from app.bot import Bot
    from app.panel import server as monitor
    from app.panel.state import MonitorState, attach_log_handler, detach_log_handler
    from app.wechat.bridge import Bridge

    lock = _acquire_lock()
    if lock is None:
        print("已经有一个 deepChat 在跑了，别开第二个。")
        return 1

    # 状态收集要在 Bot 之前建好：Bot 一初始化就开始往里写东西
    state = MonitorState()
    handler = attach_log_handler(state)
    bot = Bot(cfg, bridge=Bridge(cfg), state=state)
    bot.start()
    panel = _start_panel(cfg, bot, state)
    print("=" * 52)
    print(f" deepChat v1 已启动（{bot.me}）")
    print(" 窗口别关；Ctrl+C 停止")
    print("=" * 52)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        bot.stop()
        monitor.stop(panel)
        detach_log_handler(handler)
    return 0


def cmd_web(cfg) -> int:
    """只开后台面板，不接微信。改完参数落 override.yaml，下次启动生效。"""
    from app.panel import server as monitor
    from app.panel.state import MonitorState, attach_log_handler, detach_log_handler

    if not bool(cfg.get("monitor.enabled", True)):
        print("后台面板在 config 里被关掉了（monitor.enabled: false）")
        return 1

    state = MonitorState()
    handler = attach_log_handler(state)   # 不然面板「日志」分区永远是空的
    app = monitor.create_app(cfg, None, state)
    server = monitor.start_in_thread(
        app, str(cfg.get("monitor.host", "127.0.0.1")), int(cfg.get("monitor.port", 8787))
    )
    if server is None:
        print(f"面板起不来：{_panel_address(cfg)} 可能被占用（机器人正在跑？直接开浏览器访问它）")
        return 1
    print("=" * 52)
    print(f" 后台面板：{_panel_address(cfg)}")
    print(" 只改配置，机器人没在跑；Ctrl+C 退出")
    print("=" * 52)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        monitor.stop(server)
        detach_log_handler(handler)
    return 0


def cmd_open(cfg) -> int:
    """用默认浏览器打开后台面板。面板没在跑就说清楚该按哪个。"""
    import webbrowser

    host = str(cfg.get("monitor.host", "127.0.0.1"))
    port = int(cfg.get("monitor.port", 8787))
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    sock = socket.socket()
    sock.settimeout(0.5)
    alive = sock.connect_ex((probe_host, port)) == 0
    sock.close()
    if not alive:
        print(f"面板没在跑（{_panel_address(cfg)} 没人听）")
        print("  方式一：双击 start.bat 跑起来 —— 面板跟着机器人一起起")
        print("  方式二：跑 run.py web —— 只开面板，不接微信")
        return 1
    webbrowser.open(_panel_address(cfg))
    print(f"已打开：{_panel_address(cfg)}")
    return 0


MENU = """
  1) 启动机器人     正式跑，窗口别关（后台面板跟着起）   ← 直接回车
  2) 发送自检       给「文件传输助手」真发一条，验通道
  3) 环境自检       doctor：向量 / 模型 / 微信 / 工具全查一遍
  4) 建立资料索引   扫 knowledge/ 里的资料进资料库
  5) 打开后台面板   浏览器开 http://127.0.0.1:8787
  q) 退出
"""


def cmd_menu(cfg) -> int:
    """不带参数时的交互菜单。双击 start.bat 进的就是这里。"""
    print("=" * 52)
    print(" deepChat v1")
    print("=" * 52)
    print(MENU)
    try:
        choice = input("选几：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    if choice in ("", "1"):
        return cmd_run(cfg)
    if choice == "q":
        return 0
    if choice == "2":
        return cmd_selftest(cfg)
    if choice == "3":
        return cmd_doctor(cfg)
    if choice == "4":
        return cmd_index(cfg, rebuild=True)
    if choice == "5":
        return cmd_open(cfg)
    print("没这个选项。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="deepChat v1")
    parser.add_argument("command", nargs="?", default=None,
                        choices=["run", "ask", "doctor", "index", "web", "open",
                                 "selftest"])
    parser.add_argument("text", nargs="*", help="ask 的内容")
    parser.add_argument("--force", action="store_true", help="index 时全量重建")
    args = parser.parse_args()

    cfg = settings.load(ROOT)
    setup_log(
        cfg.get("log.level", "INFO"),
        cfg.rel(cfg.get("log.file", "data/bot.log")),
        wechat_debug=bool(cfg.get("log.wechat_debug", False)),
    )

    if args.command is None:
        return cmd_menu(cfg)
    if args.command == "ask":
        text = " ".join(args.text).strip()
        if not text:
            print("用法：run.py ask 你的问题")
            return 1
        return cmd_ask(cfg, text)
    if args.command == "doctor":
        return cmd_doctor(cfg)
    if args.command == "selftest":
        return cmd_selftest(cfg)
    if args.command == "index":
        return cmd_index(cfg, rebuild=args.force)
    if args.command == "web":
        return cmd_web(cfg)
    if args.command == "open":
        return cmd_open(cfg)
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main())
