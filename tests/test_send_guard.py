# -*- coding: utf-8 -*-
"""回归：发送护栏（「同一条回复被发两遍」的修复）。

    .venv\\Scripts\\python.exe tests\\test_send_guard.py

护栏的判据是**调用令牌**（一次 `send_text` = 一个令牌），不是「N 秒内同文本」。
所以用例分两类：**同一次调用内**该拦、**跨调用**该放。

验十七组东西，**每条都是断言**：

  1. 不装护栏时，复刻「判失败 → 重贴同一文本 → 再回车」，一次发送真实写库 ≥2 次
     （原始 Bug；这条不成立就说明假 GUI 没复刻对，后面全白测）
  2. 装护栏后同场景只写 1 次，且 `send_text` 返回 True
  3. 假阴性修正：库的像素探针说「输入框没清空」，但 UIA 说发前有字、发后已空
     → 判成功，库的重试循环压根不进入
  4. **反例**：UIA 读不到输入框值（`_get_uia()` 返回 None / 控件不暴露 Value）
     → 必须退回库原来的结论（False），绝不能把真失败误判成成功
  5. 真失败（发前没字、发后也没字）仍返回 False，且不被去重拦掉
  6. **跨调用**同文本都发出去。★ 这条是上一版的**反向回归**：按「10 秒窗口」
     去重会把一条回复里内容相同的第 2、3 个气泡静默吞掉
  7. 跨调用不同文本都发（标点不同也算不同）
  8. 多会话不互相影响：不同 chat_id 的同文本都发
  9. 归一化：`_norm_text` 折叠空白、不动标点；**同一次调用内**空白差异算同一句
 10. 不同线程各自调用都必须发
 11. `send_dedupe_seconds=0` → 关掉去重（同一次调用内硬按两次也照发），
     但假阴性修正仍然生效
 12. **令牌不看时间**：同一次调用内隔 0.3s 再按一次（远大于 0.05s 的窗口值）
     仍然拦得住 —— 说明判据是令牌不是时间
 13. 配置链路：`config.yaml` 有该项 → `schema.FIELDS` 有该项且在 chat 分组 →
     `Bridge.dedupe_seconds()` 读得到 → 改 cfg 后立刻生效（热改）
 14. 护栏幂等：重复调 `_install_send_guard` 不套娃（UIA 读取次数不翻倍）
 15. `send_text(..., guard=False)`（自检路径）：护栏整套绕过，事后状态复位
 16. ★ `split_reply` 切出 3 个全同气泡 → 必须写 3 次（上一版会只写 1 次）
 17. 按真 `GuiA.send_msg` 结构（fast 路径 + OCR 3 轮）复核护栏在**生产路径**上
     到底哪一半在生效，并把 OCR-only 的已知限制（R2）钉成断言

隔离：不联网、不调模型、不开微信、**一个字节都不写 data/**。
假 GUI 全在内存里；唯一的「真实文件」是只读打开的 `config.yaml`。
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.panel import schema
from app.settings import Settings
from app.wechat.bridge import (
    DEFAULT_DEDUPE_SECONDS,
    LANDED_GAP,
    LANDED_TRIES,
    Bridge,
    _norm_text,
)
from app.wechat.reply_split import split_reply
from _support import FAILS, check, line, total

LOGGER_NAME = "app.wechat.bridge"
_CTRL = object()   # 假装是 UIA 的输入框控件



class FakeCfg:
    """够用的配置壳：只实现 get("a.b.c", default)，且可变（验热改）。"""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class FakeUia:
    """只实现护栏用到的那两个方法：`_chat_input` / `_read_edit_value`。

    mode 决定「读得到 / 读不到」：
      - ok       正常读（返回输入框真实内容）
      - none     `_get_uia()` 返回 None（UIA 根本没起来 = OCR-only）
      - noctrl   拿不到输入框控件
      - novalue  控件不暴露 Value（`_read_edit_value` 返回 None）
    """

    def __init__(self, mode: str, gui: "FakeGui") -> None:
        self._mode = mode
        self._gui = gui

    def _chat_input(self):
        return None if self._mode == "noctrl" else _CTRL

    def _read_edit_value(self, ctrl) -> Optional[str]:
        self._gui.reads += 1          # 护栏每读一次输入框就 +1（验幂等用）
        if self._mode == "novalue":
            return None
        return self._gui._input


class FakeGui:
    """复刻 wechatauto `GuiA` 里和本次 Bug 有关的三个动作。

    真实的 GuiA：
      - `input_text()`  粘贴进输入框，并记下 `_pending_text` 给 `click_send` 回读
      - `click_send()`  用像素探针 `_input_box_has_text()` 判断「输入框清空没有」
      - `send_msg()`    `for attempt in range(3): input_text -> click_send`

    三者都在**同一次调用、同一个线程**里（这是护栏「同一次调用」判据的依据）。

    让 `probe_stuck=True` 就得到事故现场：回车已经把消息写进微信库、输入框
    也确实空了，但像素探针被光标/残留气泡带偏，仍报「输入框有字」。
    """

    def __init__(self, probe_stuck: bool = False, uia_mode: str = "ok",
                 paste_lands: bool = True) -> None:
        self.main_hwnd = 0            # 让 Bridge._ensure_window() 立刻返回，不碰 ctypes
        self._current_chat = ""
        self._pending_text = None
        self._input = ""              # 输入框里的真实内容（UIA 读的就是它）
        self._entered = False         # 本轮是否已经按过回车
        self._probe_stuck = probe_stuck
        self._uia_mode = uia_mode
        self._paste_lands = paste_lands
        self.db_lands = True          # 回车之后消息是否真的落进微信库
        self.writes = 0               # 真的写进微信库的条数
        self.db_rows = 0              # 假微信库里「这条文本」的行数（供水位/回读判定）
        self.attempts = 0             # send_msg 外层循环跑了几轮
        self.enters = 0
        self.ocr_miss = 0
        self.reads = 0                # 护栏读了几次 UIA 输入框
        self.watermark_calls = 0      # 护栏拍了几次库水位
        self.landed_calls = 0         # 护栏回读了几次库

    # -- 护栏的第三条证据：默认没有（模拟老版 wechatauto，见 DbGui）---------
    # 故意不在本类定义 _send_watermark / _wait_landed：护栏靠 hasattr 判断能力，
    # 没有就跳过这一层。要验这一层请用 DbGui。

    # -- 护栏的第二条证据 ------------------------------------------------
    def _get_uia(self):
        return None if self._uia_mode == "none" else FakeUia(self._uia_mode, self)

    # -- 像素探针 --------------------------------------------------------
    def _probe_has_text(self) -> bool:
        if self._entered and self._probe_stuck:
            return True      # 假阳性：回车后仍报「输入框有字」
        return bool(self._input)

    # -- 真 GuiA 的三个动作 ----------------------------------------------
    def input_text(self, text: str, box: Any = None, fast: bool = False) -> bool:
        self._pending_text = text
        self._entered = False
        self._input = text if self._paste_lands else ""
        return self._paste_lands

    def click_send(self, fast: bool = False) -> bool:
        pending = self._pending_text       # 真库在这里一次性消费待发文本
        self._pending_text = None
        if not self._probe_has_text():
            return False                   # 发送前无字 → 空发，判失败
        self.enters += 1
        self._entered = True
        if self._input:
            if self.db_lands:              # 只有真的落库才算写进去
                self.writes += 1
                self.db_rows += 1
                self._record_write(pending or self._input, self._current_chat)
            self._input = ""               # 微信把输入框清空
        if not self._probe_has_text():
            return True
        # 探针说还没清空 → 回落 OCR 点「发送」按钮（假 GUI 里永远找不到）
        self.ocr_miss += 1
        return False

    # -- 落库钩子：默认没有库（老版 wechatauto 也读不到库），见 DbGui --------
    def _record_write(self, text: str, who: str) -> None:
        return

    def send_msg(self, text: str, who: Optional[str] = None, **kw: Any) -> bool:
        """复刻 `GuiA.send_msg` 的 OCR 分支：`for attempt in range(3)`。"""
        self._current_chat = who or ""
        for _ in range(3):
            self.attempts += 1
            if not self.input_text(text):
                continue
            if self.click_send():
                return True
        return False


class DbGui(FakeGui):
    """新版 wechatauto：`_send_watermark` / `_wait_landed` 可用。

    这是护栏的**第三条证据**（2026-10-27 补的 R2 修复）：UIA 读不到输入框时，
    去问微信自己的数据库——写之前拍水位，写之后回读，只认水位之后的新行。
    签名与真实 `GuiA` 对齐（`guia.py:2103` / `:2128` / `:2146`）：
    `_send_watermark(who) -> dict|None`、`_wait_landed(text, who, after, mode='exact')`。

    这里把库建成一张**行表** `(会话, 序号, 正文)`，而不是一个计数——因为真库的
    三个门槛都必须验到：只认**同一会话**、只认**水位之后**的新行、正文**逐字相等**
    （`mode='exact'`）。少任何一个，都会把「别的会话说过同样的话」「上次发过同样的
    话」「粘贴接在草稿后面」当成这次成功。

    `wm_empty` / `wm_breaks` / `landed_breaks` 验「证据取不到时必须退回 False」。
    """

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.rows: List[Tuple[str, int, str]] = []   # (会话, 序号, 正文)
        self.seq = 0                                  # 全局自增序号
        self.wm_empty = False         # 新会话、库里还没行 → 水位为 None
        self.wm_breaks = False        # 拍水位抛异常（库被锁 / 拿不到密钥）
        self.landed_breaks = False    # 回读抛异常（老路径）
        self.verify_breaks = False    # 单次回读抛异常（主路径）
        self.corrupt_write = False    # 落库的正文被污染（模拟「粘贴接在草稿后面」）
        self.marks: List[int] = []    # 每次拍水位时的行号（验「拍在写之前」）
        self.verify_calls = 0         # 主路径（_verify_sent）被调次数
        self.verify_modes: List[str] = []   # 主路径收到的 mode（必须一直是 exact）
        self.landed_calls = 0         # 兜底路径（_wait_landed）被调次数
        self.landed_modes: List[str] = []

    # 库里直接塞一行历史消息（模拟「上次发过同样的话」/「别的会话说过」）
    def seed(self, who: str, text: str) -> None:
        self.seq += 1
        self.rows.append((who, self.seq, text))

    def _record_write(self, text: str, who: str) -> None:
        self.seq += 1
        body = "草稿" + text if self.corrupt_write else text
        self.rows.append((who, self.seq, body))

    def _send_watermark(self, who: Optional[str] = None):
        self.watermark_calls += 1
        mine = [(s, t) for (w, s, t) in self.rows if w == who]
        self.marks.append(max([s for s, _ in mine], default=0))
        if self.wm_breaks:
            raise RuntimeError("库读不到（密钥/被锁）")
        if self.wm_empty or not mine:
            return None                   # 真库：这个会话一行都没有 → 拍不到水位
        return {"username": who,
                "seq": max(s for s, _ in mine),
                "ids": set(mine)}

    def _scan(self, text: str, who: Optional[str], after: Optional[Dict[str, Any]],
              mode: str) -> bool:
        """真库 `_verify_sent` 的三个门槛：同会话 / 水位之后 / 正文判定。"""
        for (w, s, t) in self.rows:
            if who is not None and w != who:
                continue                  # 别的会话说的话不算
            if after:
                if s <= int(after.get("seq") or 0):
                    continue              # 水位之前的旧行不算
                if (s, t) in after.get("ids", set()):
                    continue              # 水位里已有的行不算
            if ((t == text) if mode == "exact" else (text in t)):
                return True
        return False

    # 主路径：单次回读（新版库有它，护栏用它做「自控时」的轮询）
    def _verify_sent(self, text: str, who: Optional[str] = None,
                     mode: str = "exact",
                     after: Optional[Dict[str, Any]] = None) -> bool:
        self.verify_calls += 1
        self.verify_modes.append(mode)
        if self.verify_breaks:
            raise RuntimeError("回读炸了")
        return self._scan(text, who, after, mode)

    # 兜底路径：老版库只有它（护栏在拿不到 `_verify_sent` 时才用）
    def _wait_landed(self, text: str, who: Optional[str] = None,
                     after: Optional[Dict[str, Any]] = None,
                     mode: str = "exact") -> bool:
        self.landed_calls += 1
        self.landed_modes.append(mode)
        if self.landed_breaks:
            raise RuntimeError("回读炸了")
        return self._scan(text, who, after, mode)


class FaithfulGui(FakeGui):
    """按 `guiA.send_msg` 的**真实结构**走：fast 路径 1 次 + OCR 循环 3 次。

    用来回答「护栏的两半在生产路径上各自起什么作用」——`send_msg` 一旦某次
    `click_send` 返回 True 就立刻 return，所以「同一次调用内先记账、后又要求
    写」这个序列在当前库版本里不存在。这一组断言把这个事实钉住。
    """

    def send_msg(self, text: str, who: Optional[str] = None, **kw: Any) -> bool:
        self._current_chat = who or ""
        # L1999-2003 fast 路径
        if (who == getattr(self, "_current_chat", None)
                and self.input_text(text, fast=True)
                and self.click_send(fast=True)):
            return True
        # L2036-2059 OCR 循环
        for _ in range(3):
            self.attempts += 1
            if not self.input_text(text):
                continue
            if self.click_send():
                return True
        return False


class DoublePressWx:
    """同一次 `send_text` 调用里，库**硬要写两遍**（验令牌判据本身）。

    `second` 可以指定第二次按的文本（默认与第一次相同），用来验「归一化后同一句」。

    注意：当前 `GuiA.send_msg` 不会产生这个序列（某次 `click_send` 返回 True 就
    立刻 return），所以这是「护栏机制自洽性」的测试，不是复刻现网。现网复刻见
    `FaithfulGui`。
    """

    def __init__(self, gui: FakeGui, second: Optional[str] = None,
                 gap: float = 0.0) -> None:
        self._gui = gui
        self._second = second
        self._gap = gap

    def SendMsg(self, msg: str, **kw: Any):
        self._gui._current_chat = kw.get("who") or ""
        ok1 = self._gui.input_text(msg) and self._gui.click_send()
        if self._gap:
            time.sleep(self._gap)      # 故意拖过「窗口」值，验判据不是时间
        nxt = msg if self._second is None else self._second
        ok2 = self._gui.input_text(nxt) and self._gui.click_send()
        return SimpleNamespace(is_success=bool(ok1 or ok2))


class FakeWeChat:
    """`wechatauto.WeChat` 的最小壳：`SendMsg` → `gui.send_msg`。"""

    def __init__(self, gui: FakeGui) -> None:
        self._gui = gui

    def SendMsg(self, msg: str, **kw: Any):
        ok = self._gui.send_msg(msg, who=kw.get("who"))
        return SimpleNamespace(is_success=ok)


class Capture(logging.Handler):
    """抓住 `app.wechat.bridge` 的日志，用来断言「打了 / 没打」某一行。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[Tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append((record.levelno, record.getMessage()))
        except Exception:
            pass

    def hit(self, needle: str, level: Optional[int] = None) -> int:
        return sum(1 for lv, text in self.records
                   if needle in text and (level is None or lv == level))


class logs(Capture):
    """`with logs() as cap:` —— 临时挂到 bridge 的 logger 上。"""

    def __enter__(self) -> "logs":
        self._lg = logging.getLogger(LOGGER_NAME)
        self._old = self._lg.level
        self._lg.addHandler(self)
        self._lg.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._lg.removeHandler(self)
        self._lg.setLevel(self._old)


def new_env(stuck: bool = False, uia: str = "ok", paste_lands: bool = True,
            dedupe: float = DEFAULT_DEDUPE_SECONDS, cls: type = FakeGui):
    """一套干净的护栏环境：假 GUI + 假通道 + 真 Bridge（不联网、不开微信）。"""
    gui = cls(probe_stuck=stuck, uia_mode=uia, paste_lands=paste_lands)
    cfg = FakeCfg({"wechat": {"send_dedupe_seconds": dedupe}})
    bridge = Bridge(cfg)
    wx = FakeWeChat(gui)
    bridge._wx = wx                    # 直接塞进去，绕开 _wechat() 的真初始化
    bridge.display_name = lambda u: u  # 绕开 db 查询
    bridge._install_send_guard(wx)
    return bridge, gui, cfg


def send(bridge: Bridge, text: str, chat: str = "chat1", guard: bool = True) -> bool:
    """走一遍真 `Bridge.send_text`（含护栏 + 台账 + bypass），但不碰真微信。"""
    return bridge.send_text(chat, text, guard=guard)


def _guia_params(func: str) -> Optional[List[str]]:
    """从 wechatauto/guia.py **源码文本**里抠出某个方法的形参名（不 import）。

    为什么不 import：`wechatauto.guia` 会拉起 uiautomation/COM，测试里一旦
    初始化 COM 就可能挂住（实测 60s 未返回），还会碰真实微信的 UIA 树。
    这里只 `find_spec` 定位包目录、读文本、正则取形参，纯只读。
    """
    try:
        spec = importlib.util.find_spec("wechatauto")
        if spec is None or not spec.origin:
            return None
        src = (Path(spec.origin).parent / "guia.py").read_text(encoding="utf-8")
    except Exception:
        return None
    hit = re.search(rf"def\s+{func}\s*\(([^)]*)\)", src, re.S)
    if not hit:
        return None
    out: List[str] = []
    for raw in hit.group(1).split(","):
        name = raw.split(":")[0].split("=")[0].strip()
        if name and name != "self":
            out.append(name)
    return out


# ====================================================================
# 1. 复现原始 Bug（不装护栏）
# ====================================================================
def case_repro(stuck: bool) -> None:
    line("1. 不装护栏：复刻「判失败 → 重贴同一文本 → 再回车」，一次发送写库两次")
    gui = FakeGui(probe_stuck=stuck)
    cfg = FakeCfg({"wechat": {"send_dedupe_seconds": DEFAULT_DEDUPE_SECONDS}})
    bridge = Bridge(cfg)
    bridge._wx = FakeWeChat(gui)
    ok = gui.send_msg("今天下午三点开会，别忘了带材料", who="chat1")

    check("假 GUI 里探针假阳性生效（回车后仍报「输入框有字」）",
          stuck and gui.ocr_miss > 0, f"ocr_miss={gui.ocr_miss}")
    check("不装护栏时 send_msg 返回 False（我们这层以为没发出去）",
          ok is False, f"返回={ok}")
    check("不装护栏时外层重试循环真的跑了多轮",
          gui.attempts >= 2, f"attempts={gui.attempts}")
    check("★ 原始 Bug 复现：真实写库 ≥2 次（同一条一模一样）",
          gui.writes >= 2, f"writes={gui.writes}")
    check("护栏没装时 click_send 没被替换",
          type(gui).click_send is FakeGui.click_send, "")


# ====================================================================
# 2 + 3. 装护栏之后：只写一次 + 假阴性修正
# ====================================================================
def case_guard_fixes(stuck: bool) -> None:
    line("2. 装护栏后同场景：只写 1 次，且 send_text 返回 True")
    bridge, gui, _ = new_env(stuck=stuck)
    with logs() as cap:
        ok = send(bridge, "今天下午三点开会，别忘了带材料")

    check("★ 装护栏后真实写库只 1 次", gui.writes == 1, f"writes={gui.writes}")
    check("装护栏后 send_text 返回 True（成功判定没有反向）", ok is True, f"返回={ok}")

    line("3. 假阴性修正：探针说没清空、UIA 说发前有字发后已空 → 判成功")
    check("★ 库的重试循环没进入第二轮（attempts == 1）",
          gui.attempts == 1, f"attempts={gui.attempts}")
    check("确实按过回车（真的发出去了，不是空判成功）",
          gui.enters == 1, f"enters={gui.enters}")
    check("日志里写清了「发前有字、发后已空，不再重发」",
          cap.hit("发前输入框有字、发后已空") == 1,
          f"命中={cap.hit('发前输入框有字、发后已空')}")
    check("修正后没有误报「疑似重复发送」",
          cap.hit("疑似重复发送") == 0, f"命中={cap.hit('疑似重复发送')}")


# ====================================================================
# 4. 反例：证据不足时必须退回 False
# ====================================================================
def case_no_evidence() -> None:
    line("4. 反例：UIA 读不到输入框值 → 必须退回 False，不许误判成功")
    for mode, label in (("none", "_get_uia() 返回 None"),
                        ("noctrl", "拿不到输入框控件"),
                        ("novalue", "控件不暴露 ValuePattern")):
        bridge, gui, _ = new_env(stuck=True, uia=mode)
        ok = send(bridge, "证据不足的一句话")
        check(f"★ {label} → send_text 返回 False（不误判成功）",
              ok is False, f"返回={ok}")

    bridge, gui, _ = new_env(stuck=True, uia="none")
    ok = send(bridge, "证据不足的一句话")
    check("读不到 UIA 时不会假成功（writes 有，但返回 False）",
          gui.writes >= 1 and ok is False, f"writes={gui.writes} 返回={ok}")
    check("读不到 UIA 时退回库原来的重试行为",
          gui.attempts >= 2, f"attempts={gui.attempts}")

    line("4b. 读不到输入框时进程内只记一条 INFO（以后降 DEBUG）")
    bridge, gui, _ = new_env(stuck=True, uia="none")
    with logs() as cap:
        for _ in range(2):
            send(bridge, "读不到输入框的两次尝试")
    check("★ 「读不到输入框内容」正好一条 INFO（不刷屏）",
          cap.hit("读不到输入框内容", logging.INFO) == 1,
          f"INFO 命中={cap.hit('读不到输入框内容', logging.INFO)}")
    check("INFO 里写清了原因（OCR-only 的假阴性修正不生效）",
          cap.hit("假阴性修正") == 1, f"命中={cap.hit('假阴性修正')}")


# ====================================================================
# 5. 真失败
# ====================================================================
def case_real_failure() -> None:
    line("5. 真失败（发前没字、发后也没字）：仍返回 False，且不被去重拦掉")
    bridge, gui, _ = new_env(stuck=False, paste_lands=False)
    ok1 = send(bridge, "粘贴根本落空的一句话")
    rounds_after_first = gui.attempts
    ok2 = send(bridge, "粘贴根本落空的一句话")
    check("真失败时 send_text 返回 False", ok1 is False and ok2 is False,
          f"{ok1}/{ok2}")
    check("真失败时一条都没写进库", gui.writes == 0, f"writes={gui.writes}")
    check("★ 真失败没被去重拦掉（第二次仍然真的去试了库的重试循环）",
          gui.attempts > rounds_after_first,
          f"第一次后 attempts={rounds_after_first}，第二次后 attempts={gui.attempts}")


# ====================================================================
# 6. 跨调用同文本都发（上一版反向回归）
# ====================================================================
def case_cross_call_same_text() -> None:
    line("6. 跨调用同文本都发出去（★ 上一版按 10 秒窗口去重会吞掉第 2、3 条）")
    bridge, gui, _ = new_env()
    with logs() as cap:
        rets = [send(bridge, "在的，怎么了") for _ in range(3)]
    check("★ 三次独立调用，真实写库 3 次", gui.writes == 3, f"writes={gui.writes}")
    check("三次都返回 True", rets == [True, True, True], f"{rets}")
    check("★ 跨调用不再打「疑似重复发送」（拦错的对象没了）",
          cap.hit("疑似重复发送") == 0, f"命中={cap.hit('疑似重复发送')}")


# ====================================================================
# 7. 不同文本
# ====================================================================
def case_different_text() -> None:
    line("7. 跨调用不同文本都发出去（标点不同也算不同）")
    bridge, gui, _ = new_env()
    send(bridge, "在的，怎么了")
    send(bridge, "我这边刚忙完")
    check("★ 两句不同的话都发出去", gui.writes == 2, f"writes={gui.writes}")

    bridge, gui, _ = new_env()
    send(bridge, "好。")
    send(bridge, "好？")
    check("★ 「好。」和「好？」不被当成同一条（标点不归一）",
          gui.writes == 2, f"writes={gui.writes}")


# ====================================================================
# 8. 多会话
# ====================================================================
def case_multi_chat() -> None:
    line("8. 多会话不互相影响：不同 chat_id 的同文本都发")
    bridge, gui, _ = new_env()
    send(bridge, "收到", chat="chatA")
    send(bridge, "收到", chat="chatB")
    check("★ A、B 两个会话的同一句话都发出去", gui.writes == 2, f"writes={gui.writes}")


# ====================================================================
# 9. 归一化
# ====================================================================
def case_normalize() -> None:
    line("9. 归一化：折叠空白、不动标点；同一次调用内空白差异算同一句")
    check("_norm_text 折叠全部空白", _norm_text("  a \n b\t c ") == "a b c",
          repr(_norm_text("  a \n b\t c ")))
    check("_norm_text 不动标点", _norm_text("好。") != _norm_text("好？"), "")
    check("_norm_text 对空白差异给出同一个 key",
          _norm_text("  今天  天气   不错  ") == _norm_text("今天 天气 不错"), "")

    bridge, gui, _ = new_env()
    send(bridge, "  今天  天气   不错  ")
    send(bridge, "今天 天气 不错")
    check("跨调用时不合并（两次都发）", gui.writes == 2, f"writes={gui.writes}")

    # 同一次调用内，先贴带多余空白的，再贴规整过的 —— 归一化后是同一句
    gui = FakeGui()
    bridge = Bridge(FakeCfg({"wechat": {"send_dedupe_seconds": 10}}))
    wx = DoublePressWx(gui, second="今天 天气 不错")
    bridge._wx = wx
    bridge.display_name = lambda u: u
    bridge._install_send_guard(wx)
    ok = send(bridge, "  今天  天气   不错  ")
    check("★ 同一次调用内，空白差异算同一句（第二次被拦，只写 1 次）",
          gui.writes == 1 and ok is True, f"writes={gui.writes} 返回={ok}")


# ====================================================================
# 10. 不同线程
# ====================================================================
def case_cross_thread() -> None:
    line("10. 不同线程各自调用都必须发（连问两遍不能被吞）")
    bridge, gui, _ = new_env()
    barrier = threading.Barrier(2)
    results: Dict[int, Any] = {}
    idents: List[int] = []

    def run(tag: int) -> None:
        barrier.wait()                 # 两个线程同时在世，线程号必然不同
        idents.append(threading.get_ident())
        results[tag] = send(bridge, "在吗")

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    check("两个线程的线程号确实不同", len(set(idents)) == 2, f"idents={idents}")
    check("★ 不同线程的同文本都发出去（写库 2 次）", gui.writes == 2, f"writes={gui.writes}")
    check("两次都返回 True", sorted(results.values()) == [True, True], f"{results}")


# ====================================================================
# 11. dedupe = 0
# ====================================================================
def case_dedupe_off() -> None:
    line("11. send_dedupe_seconds=0 → 关掉去重，但仍修正假阴性")
    gui = FakeGui()
    bridge = Bridge(FakeCfg({"wechat": {"send_dedupe_seconds": 0}}))
    wx = DoublePressWx(gui)
    bridge._wx = wx
    bridge.display_name = lambda u: u
    bridge._install_send_guard(wx)
    ok = send(bridge, "关掉保护的一句话")
    check("★ 开关为 0 时同一次调用内硬按两次也照发（写库 2 次）",
          gui.writes == 2, f"writes={gui.writes}")
    check("调用整体仍报成功", ok is True, f"返回={ok}")

    bridge, gui, _ = new_env(stuck=True, dedupe=0)
    ok = send(bridge, "关掉保护也要修正假阴性")
    check("★ 开关为 0 时假阴性修正仍然生效（只写 1 次）",
          gui.writes == 1 and ok is True and gui.attempts == 1,
          f"writes={gui.writes} 返回={ok} attempts={gui.attempts}")

    # 兜底路径（无令牌）也必须被 0 关掉 —— 走 wx.SendMsg 绕开 send_text 就没有令牌
    gui = FakeGui()
    bridge = Bridge(FakeCfg({"wechat": {"send_dedupe_seconds": 0}}))
    wx = DoublePressWx(gui)
    bridge._wx = wx
    bridge.display_name = lambda u: u
    bridge._install_send_guard(wx)
    with logs() as cap:
        wx.SendMsg("无令牌 + 开关为 0")
    check("★ 开关为 0 时**兜底路径**也不再拦（无令牌 + 同线程 → 写库 2 次）",
          gui.writes == 2, f"writes={gui.writes}")
    check("兜底路径被 0 关掉后不打「疑似重复发送」",
          cap.hit("疑似重复发送") == 0, f"命中={cap.hit('疑似重复发送')}")


# ====================================================================
# 12. 令牌不看时间
# ====================================================================
def case_token_not_time() -> None:
    line("12. 判据是令牌不是时间：同一次调用内隔 0.3s 再按，照样拦得住")
    # 把开关值调到 0.05s（远小于 0.3s）。按时间判会放行，按令牌判会拦住。
    gui = FakeGui()
    bridge = Bridge(FakeCfg({"wechat": {"send_dedupe_seconds": 0.05}}))
    wx = DoublePressWx(gui, gap=0.3)
    bridge._wx = wx
    bridge.display_name = lambda u: u
    bridge._install_send_guard(wx)
    with logs() as cap:
        ok = send(bridge, "隔了 0.3 秒又按一次")
    check("★ 隔 0.3s（> 0.05s）仍然只写 1 次 → 令牌判据不看时间",
          gui.writes == 1, f"writes={gui.writes}")
    check("★ 这次确实被「疑似重复发送」拦下并记了日志",
          cap.hit("疑似重复发送") == 1, f"命中={cap.hit('疑似重复发送')}")
    check("日志里写清了命中的是令牌路径",
          cap.hit("token 命中") == 1, f"命中={cap.hit('token 命中')}")
    check("日志里带了字数（方便对着微信库核）",
          cap.hit("字）") == 1, f"命中={cap.hit('字）')}")
    check("整体返回 True（被拦的那次也算成功）", ok is True, f"返回={ok}")

    line("12b. 无令牌时的「同线程 + 窗口」兜底路径（防御性，正常流程走不到）")
    gui = FakeGui()
    bridge = Bridge(FakeCfg({"wechat": {"send_dedupe_seconds": 10}}))
    wx = DoublePressWx(gui)
    bridge._wx = wx
    bridge.display_name = lambda u: u
    bridge._install_send_guard(wx)
    with logs() as cap:
        wx.SendMsg("绕过 send_text 直接按两次")     # 没有令牌 → 走兜底
    check("无令牌 + 同线程 + 窗口内 → 兜底仍然拦得住（写库 1）",
          gui.writes == 1, f"writes={gui.writes}")
    check("日志写明命中原因是「窗口兜底」",
          cap.hit("窗口兜底") == 1, f"命中={cap.hit('窗口兜底')}")

    gui = FakeGui()
    bridge = Bridge(FakeCfg({"wechat": {"send_dedupe_seconds": 0.05}}))
    wx = DoublePressWx(gui, gap=0.3)
    bridge._wx = wx
    bridge.display_name = lambda u: u
    bridge._install_send_guard(wx)
    wx.SendMsg("无令牌且窗口已过")                  # 无令牌 + 超窗 → 放行
    check("无令牌 + 超窗 → 兜底放行（写库 2）——这正是旧判据会误判的地方",
          gui.writes == 2, f"writes={gui.writes}")


# ====================================================================
# 13. 配置链路
# ====================================================================
def case_config_chain() -> None:
    line("13. 配置链路：config.yaml → schema.FIELDS(chat) → dedupe_seconds() → 热改")
    yaml_cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    value = (yaml_cfg.get("wechat") or {}).get("send_dedupe_seconds")
    check("★ config.yaml 里有 wechat.send_dedupe_seconds", value is not None, f"值={value}")
    check("config.yaml 里的值是 10",
          isinstance(value, (int, float)) and float(value) == 10.0, f"值={value}")

    field = next((f for f in schema.FIELDS
                  if f.path == "wechat.send_dedupe_seconds"), None)
    check("★ schema.FIELDS 里有这一项", field is not None, "")
    if field is not None:
        check("分组是 chat（面板上落在「聊天」页）", field.group == "chat", field.group)
        check("类型是 float", field.kind == "float", field.kind)
        check("范围 0 ~ 120（0 = 关掉保护）",
              (field.minimum, field.maximum) == (0.0, 120.0),
              f"{field.minimum}~{field.maximum}")
        check("不需要重启才生效（restart=False）", field.restart is False, str(field.restart))
        # 配置名保留、语义靠 label/hint 纠正（lead 已拍板）
        check("★ 配置项保留原名 wechat.send_dedupe_seconds（不改成 bool）",
              field.path == "wechat.send_dedupe_seconds", field.path)
        check("★ hint 写明「0 = 关」", "0 = 关" in field.hint, "")
        check("★ hint 写明拦的是「同一次发送调用内」",
              "同一次发送调用内" in field.hint, "")
        check("★ hint 明确否掉「N 秒内不许发同样的话」",
              "N 秒内" in field.hint and "不是" in field.hint, "")
        check("★ hint 写明数值只限定台账保留时长",
              "台账保留时长" in field.hint, "")

    yaml_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    check("config.yaml 的注释也写了「同一次发送调用内」",
          "同一次发送调用内" in yaml_text, "")
    check("config.yaml 的注释也写了「0 = 关掉这层拦截」",
          "0 = 关掉这层拦截" in yaml_text, "")

    real = Settings(yaml_cfg, ROOT)     # 只读；不碰 override.yaml
    check("真 Settings 读得到这一项", real.get("wechat.send_dedupe_seconds") == value,
          f"{real.get('wechat.send_dedupe_seconds')}")

    bridge = Bridge(None)
    check("cfg 为 None 时 dedupe_seconds() 退回默认窗口",
          bridge.dedupe_seconds() == DEFAULT_DEDUPE_SECONDS,
          f"{bridge.dedupe_seconds()}")

    b2, _, cfg2 = new_env(dedupe=10)
    before = b2.dedupe_seconds()
    cfg2.data["wechat"]["send_dedupe_seconds"] = 3
    after = b2.dedupe_seconds()
    check("★ 改 cfg 后 dedupe_seconds() 立刻生效（热改，不缓存）",
          before == 10.0 and after == 3.0, f"{before} → {after}")

    cfg3 = FakeCfg({"wechat": {"send_dedupe_seconds": -5}})
    check("负数被夹到 0（不会变成「永远去重」）", Bridge(cfg3).dedupe_seconds() == 0.0,
          f"{Bridge(cfg3).dedupe_seconds()}")
    cfg4 = FakeCfg({"wechat": {"send_dedupe_seconds": "不是数字"}})
    check("坏值退回默认窗口（不抛异常）",
          Bridge(cfg4).dedupe_seconds() == DEFAULT_DEDUPE_SECONDS,
          f"{Bridge(cfg4).dedupe_seconds()}")


# ====================================================================
# 14. 幂等
# ====================================================================
def case_idempotent() -> None:
    line("14. 护栏幂等：重复安装不套娃")
    bridge, gui, _ = new_env()
    first = gui.click_send
    bridge._install_send_guard(bridge._wx)
    bridge._install_send_guard(bridge._wx)
    check("★ 重复安装后 click_send 还是同一个函数对象（没有套第二层）",
          gui.click_send is first, "")
    check("_bot_send_guard 标记已置上", getattr(gui, "_bot_send_guard", False) is True, "")

    send(bridge, "套娃检测用的一句话")
    check("★ 套娃的行为特征：UIA 输入框只被读 1 次（套一层会读 2 次）",
          gui.reads == 1, f"reads={gui.reads}")
    check("正常发一条就写 1 次（嵌套没有放大写入）", gui.writes == 1, f"writes={gui.writes}")


# ====================================================================
# 15. 自检路径 guard=False
# ====================================================================
def case_selftest_bypass() -> None:
    line("15. 自检路径 send_text(..., guard=False)：护栏整套绕过，事后状态复位")
    bridge, gui, _ = new_env(stuck=True)
    ok_bypass = send(bridge, "【自检】发送通道测试", guard=False)
    check("★ guard=False 连假阴性修正一起绕过 → 退回库原判（会重发，写库 ≥2）",
          gui.writes >= 2, f"writes={gui.writes}")
    check("★ 绕过时库里判失败，所以 send_text 返回 False（自检要能看到真话）",
          ok_bypass is False, f"返回={ok_bypass}")
    check("调用结束后 bypass 开关已复位",
          getattr(bridge._guard_bypass, "on", False) is False, "")
    check("调用结束后调用令牌已复位",
          getattr(bridge._guard_bypass, "token", None) is None, "")

    bridge, gui, _ = new_env(stuck=True)
    ok_guard = send(bridge, "【自检】发送通道测试", guard=True)
    check("对照组：同样的假 GUI 走护栏 → 只写 1 次、返回 True",
          gui.writes == 1 and ok_guard is True, f"writes={gui.writes} 返回={ok_guard}")


# ====================================================================
# 16. 全同气泡（上一版被吞的地方）
# ====================================================================
def case_identical_bubbles() -> None:
    line("16. ★ split_reply 切出 3 个全同气泡 → 必须写 3 次（上一版只写 1 次）")
    para = "今天真的好累啊我先去洗个澡，明天再慢慢聊吧。"
    bubbles = split_reply("\n".join([para] * 3), 40, 3)
    check("前提：split_reply 真的切出了 3 条完全相同的气泡",
          len(bubbles) == 3 and len(set(bubbles)) == 1,
          f"{len(bubbles)} 条 / 去重后 {len(set(bubbles))} 条")

    bridge, gui, _ = new_env()
    with logs() as cap:
        rets = [send(bridge, b) for b in bubbles]
    check("★ 3 个全同气泡全部写进微信库（写库 == 3）",
          gui.writes == 3, f"writes={gui.writes}")
    check("★ 三次都返回 True，调用方 all_ok 与真实条数一致",
          rets == [True, True, True], f"{rets}")
    check("★ 没有一条被打成「疑似重复发送」",
          cap.hit("疑似重复发送") == 0, f"命中={cap.hit('疑似重复发送')}")


# ====================================================================
# 17. 生产路径复核（并钉住 OCR-only 的已知限制）
# ====================================================================
def case_production_path() -> None:
    line("17. 按真 GuiA.send_msg 结构复核：护栏哪一半在生产路径上真的在生效")
    asked = {"n": 0, "hit": 0}

    bridge, gui, _ = new_env(stuck=True, cls=FaithfulGui)
    real_hit = bridge._ledger_hit

    def spy(chat: str, text: str) -> bool:
        asked["n"] += 1
        got = real_hit(chat, text)
        asked["hit"] += 1 if got else 0
        return got
    bridge._ledger_hit = spy
    ok = send(bridge, "生产路径复核用的一句话")
    check("探针假阳性 + UIA 可读：只写 1 次、返回 True（假阴性修正在扛）",
          gui.writes == 1 and ok is True, f"writes={gui.writes} 返回={ok}")
    check("★ 令牌去重在生产路径上被问了但一次都没命中"
          "（成功即 return，库里不存在「同调用内二次写」序列）",
          asked["n"] >= 1 and asked["hit"] == 0, f"被问={asked['n']} 命中={asked['hit']}")

    line("17b. 老版库（没有 _send_watermark / _wait_landed）→ 第三条证据整层跳过")
    bridge, gui, _ = new_env(stuck=True, uia="none", cls=FaithfulGui)
    with logs() as cap:
        ok = send(bridge, "老版库上 OCR-only 的发送")
    check("★ 没有库回读能力时退回原行为：一次 send_text 写 4 条（fast 1 + OCR 3）",
          gui.writes == 4, f"writes={gui.writes}")
    check("这条路上护栏不替我们判成功（返回 False，交给原本的重试）",
          ok is False and gui.attempts == 3,
          f"返回={ok} attempts={gui.attempts}")
    check("★ 唯一可见性：日志里那条一次性 INFO 真的打出来了",
          cap.hit("读不到输入框内容", logging.INFO) == 1,
          f"INFO 命中={cap.hit('读不到输入框内容', logging.INFO)}")


# ====================================================================
# 18. 第三条证据：UIA 盲区改问微信库（2026-10-27 补的 R2 修复）
# ====================================================================
def case_db_evidence() -> None:
    line("18-0. 契约：护栏依赖的库方法签名（位置传错会直接把「库里确有」判成失败）")
    sig_verify = _guia_params("_verify_sent")
    check("★ 真库 `_verify_sent(text, who, mode, after)` —— mode 在前、after 在后",
          sig_verify is not None and sig_verify[:4] == ["text", "who", "mode", "after"],
          f"{sig_verify}")
    sig_wait = _guia_params("_wait_landed")
    check("★ 真库 `_wait_landed(text, who, after, mode)` —— 兜底路径 after 在前（和上面不同！）",
          sig_wait is not None and sig_wait[:4] == ["text", "who", "after", "mode"],
          f"{sig_wait}")
    sig_wm = _guia_params("_send_watermark")
    check("★ 真库 `_send_watermark(who)`", sig_wm == ["who"], f"{sig_wm}")
    check("假件 DbGui 的 `_verify_sent` 形参顺序和真库一致（否则测的不是同一条路）",
          [p for p in inspect.signature(DbGui._verify_sent).parameters if p != "self"]
          == ["text", "who", "mode", "after"],
          f"{list(inspect.signature(DbGui._verify_sent).parameters)}")
    check("假件 DbGui 的 `_wait_landed` 形参顺序和真库一致",
          [p for p in inspect.signature(DbGui._wait_landed).parameters if p != "self"]
          == ["text", "who", "after", "mode"],
          f"{list(inspect.signature(DbGui._wait_landed).parameters)}")

    line("18. UIA 盲区 → 问微信库：主路径是 _verify_sent 的「自控时」轮询")
    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "这个会话里本来就有的上一句")   # 有历史才有水位可比
    with logs() as cap:
        ok = send(bridge, "UIA 读不到但消息真发出去了")
    check("★ R2 修复生效：UIA 盲区 + 库里多了这条 → 只写 1 条、判成功",
          gui.writes == 1 and ok is True, f"writes={gui.writes} 返回={ok}")
    check("★ 水位是**在第一次写动作之前**拍的（记录到的是那行历史，不是新行）",
          gui.marks == [1], f"marks={gui.marks}")
    check("走的是主路径 `_verify_sent`（不是 8 秒轮询那套 `_wait_landed`）",
          gui.verify_calls >= 1 and gui.landed_calls == 0,
          f"verify={gui.verify_calls} landed={gui.landed_calls}")
    check("★ 回读用的是 mode='exact'（放宽成 contains 会把「上次发过同样的话」当成功）",
          gui.verify_modes == ["exact"], f"modes={gui.verify_modes}")
    check("日志说明「库里确实多了这条新消息」",
          cap.hit("库里确实多了这条新消息") == 1,
          f"命中={cap.hit('库里确实多了这条新消息')}")

    line("18b. 反例：库里查不到新行 → 必须退回 False，绝不「宁可少发」")
    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "这个会话里本来就有的上一句")
    gui.db_lands = False                 # 回车被吃掉，一条都没落库
    ok = send(bridge, "其实根本没发出去的一句话")
    check("★ 库回读查不到新行 → send_text 返回 False（不误判成功）",
          ok is False, f"返回={ok}")
    check("★ 一条都没写成（writes == 0）", gui.writes == 0, f"writes={gui.writes}")
    check("库确实被回读过了（不是靠猜）", gui.verify_calls >= 1,
          f"verify_calls={gui.verify_calls}")

    line("18c. 三个门槛逐个拆：会话 / 水位 / 正文逐字相等（少一个就会误判）")
    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "chat1 的旧闲聊")
    gui.seed("chat2", "这句话在 chat2 里存在")      # 同文本，但在别的会话
    gui.db_lands = False
    ok = send(bridge, "这句话在 chat2 里存在", chat="chat1")
    check("★ 门槛①会话：同文本只在 chat2 → 往 chat1 发仍判 False",
          ok is False, f"返回={ok}")

    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "上次已经发过的一句话")        # 水位之前的老行
    gui.db_lands = False
    ok = send(bridge, "上次已经发过的一句话")
    check("★ 门槛②水位：同会话的老行（水位之前）不算 → 仍判 False",
          ok is False, f"返回={ok}")

    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "chat1 的旧闲聊")
    gui.corrupt_write = True             # 落库正文变成「草稿xxx」
    ok = send(bridge, "会被拼上草稿的一句话")
    check("★ 门槛③正文：落库了但正文不等于待发文本 → 仍判 False（不放宽成包含匹配）",
          ok is False, f"返回={ok}")

    line("18d. 证据取不到时一律退回原判：抛异常 / 水位为 None")
    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "旧闲聊")
    gui.verify_breaks = True             # 单次回读抛异常
    ok = send(bridge, "回读抛异常的一句话")
    check("★ 回读抛异常 → 按没发出去处理，返回 False，"
          "且每轮库重试里只读 1 次就放弃（不在这层自我重试）",
          ok is False and gui.verify_calls == gui.attempts,
          f"返回={ok} verify_calls={gui.verify_calls} attempts={gui.attempts}")

    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "旧闲聊")
    gui.wm_breaks = True                 # 拍水位抛异常（库被锁 / 拿不到密钥）
    ok = send(bridge, "拍不到水位的一句话")
    check("★ _send_watermark 抛异常 → 水位为 None，整层跳过，退回原判（False）",
          ok is False and gui.verify_calls == 0,
          f"返回={ok} verify_calls={gui.verify_calls}")

    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    ok = send(bridge, "新会话里的第一句")          # 这个会话一行都没有
    check("★ 空会话拍不到水位（真库返回 None）→ 第一次尝试拿不到这层证据",
          gui.marks == [0, 1], f"marks={gui.marks}")
    check("★ 【R2 的边界】空会话首句仍会写 2 条（从 4 条降到 2 条），"
          "因为第二次尝试的水位已经把第一次写进去的那条算进去了",
          gui.writes == 2 and ok is True,
          f"writes={gui.writes} 返回={ok}（会话里有历史时是 1 条）")

    line("18e. 成本上界：自控时轮询，绝不用库那套 1s×8 堵住全局发送锁")
    # 给「每次 click_send」套一层计时，量它自己花了多久（含库内重试的等待）
    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "旧闲聊")
    gui.db_lands = False
    inner = gui.click_send
    per_call: List[float] = []

    def timed(fast: bool = False) -> bool:
        t0 = time.time()
        try:
            return inner(fast=fast)
        finally:
            per_call.append(time.time() - t0)
    gui.click_send = timed
    started = time.time()
    send(bridge, "失败路径的耗时探针")
    elapsed = time.time() - started

    check("★ 单次 click_send 的额外耗时 ≤ 2.0s（设计上限 3×0.5s=1.5s，留 0.5s 余量）",
          bool(per_call) and max(per_call) <= 2.0,
          f"各次={[round(x, 2) for x in per_call]}")
    check("★ 整条 send_text（库 3 次 retry）≤ 5.0s",
          elapsed <= 5.0, f"{elapsed:.2f}s")
    check("★ 每次 click_send 最多回读 LANDED_TRIES(3) 次",
          gui.verify_calls == LANDED_TRIES * gui.attempts,
          f"verify_calls={gui.verify_calls} = {LANDED_TRIES}×{gui.attempts}")
    check("★ 上界是 LANDED_TRIES × LANDED_GAP ≤ 2s（库那套 1s×8 会堵住所有会话）",
          LANDED_TRIES * LANDED_GAP <= 2.0,
          f"{LANDED_TRIES}×{LANDED_GAP}={LANDED_TRIES * LANDED_GAP:.1f}s")
    # Bot._send_one 是 0/2/5s 三次重试、每次一条 send_text，所以最坏 ≈ 3×T + 7
    worst = 3 * elapsed + (0.0 + 2.0 + 5.0)
    check("★ 折算 Bot._send_one 三次 attempt（0+2+5s 重试间隔）≤ 18s —— "
          "设计目标 ~15s，这里放到 18s 容 CI 抖动；旧实现（8s×3 轮）这条会是 ~79s",
          worst <= 18.0, f"3×{elapsed:.2f}+7 = {worst:.1f}s")

    line("18f. 老版库（只有 _wait_landed，没有 _verify_sent）→ 退回兜底路径")
    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "旧闲聊")
    gui._verify_sent = None               # 模拟老版 wechatauto：没有单次回读
    ok = send(bridge, "老版库上盲区里的一句话")
    check("★ 没有 `_verify_sent` 时退回 `_wait_landed`，仍然判得对（writes=1、True）",
          ok is True and gui.writes == 1 and gui.landed_calls >= 1,
          f"返回={ok} writes={gui.writes} landed={gui.landed_calls}")

    bridge, gui, _ = new_env(stuck=True, uia="none", cls=DbGui)
    gui.seed("chat1", "旧闲聊")
    gui._verify_sent = None
    gui.db_lands = False
    ok = send(bridge, "老版库上其实没发出去的一句话")
    check("★ 兜底路径同样不会误判成功（False）", ok is False, f"返回={ok}")

    line("18g. 正常路径：一次库都不读")
    bridge, gui, _ = new_env(stuck=True, uia="ok", cls=DbGui)
    ok = send(bridge, "UIA 读得到的正常路径")
    check("★ UIA 读得到时一次库都不读（watermark_calls == 0、verify_calls == 0）",
          gui.watermark_calls == 0 and gui.verify_calls == 0,
          f"watermark={gui.watermark_calls} verify={gui.verify_calls}")
    check("此时走 UIA 那条证据就够（writes=1、返回 True）",
          gui.writes == 1 and ok is True, f"writes={gui.writes} 返回={ok}")


def main() -> int:
    print("=" * 52)
    print("发送护栏演示：同一条回复被发两遍的修复（令牌判据）")
    print("=" * 52)

    case_repro(stuck=True)
    case_guard_fixes(stuck=True)
    case_no_evidence()
    case_real_failure()
    case_cross_call_same_text()
    case_different_text()
    case_multi_chat()
    case_normalize()
    case_cross_thread()
    case_dedupe_off()
    case_token_not_time()
    case_config_chain()
    case_idempotent()
    case_selftest_bypass()
    case_identical_bubbles()
    case_production_path()
    case_db_evidence()

    print()
    print("=" * 52)
    print("结论")
    print("=" * 52)
    print(f"  断言 {total()} 项，通过 {total() - len(FAILS)} 项，失败 {len(FAILS)} 项")
    if FAILS:
        for name in FAILS:
            print(f"  [失败] {name}")
        return 1
    print("  发送护栏可用：原始 Bug 被挡住、假阴性被修正、UIA 盲区由库回读兜底、"
          "证据不足时不误判、全同气泡不会被吞。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
