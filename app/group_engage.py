# -*- coding: utf-8 -*-
"""群聊接话判定：把「必须 @ 才理」换成「被叫到就回、熟人能接着聊」。

整个模块是**纯函数 + 内存状态**：不持锁、不调网络、不读磁盘（`MemberCache`
第一次读群成员表除外，有 TTL）。这样它才能被单测直接喂入造好的成员表和时间，
不依赖真实微信。

判定链只有一条（顺序即语义，见 `GroupEngager.judge` 的 docstring）：
  blocked -> 不回；非群 -> 恒回；engage.enabled 假 -> legacy；否则打分出 hit。

权重（100/60/50/40/30/-50/-30）写死在这里、不进面板。七个权重就是七个调参
黑洞，业务上没人能凭直觉调；面板只给开关和几个分数线。
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---- 常量（不进面板）
W_AT_ME, W_AT_UNKNOWN, W_CALLNAME = 100, 60, 50
W_CONTINUITY, W_QUESTION = 40, 30
W_AT_OTHER, W_TOO_SHORT = -50, -30
MIN_CHARS = 4

# 归一化要抹掉的空白类字符。`\s` 已经覆盖普通空白和 U+2005/U+00A0，
# 但 U+200B（零宽空格）不在 `\s` 里，微信消息里又常见，必须单独列出来。
_WS_RE = re.compile(r"[\s\u2005\u00a0\u200b]")

# 中文称呼的「呼语位置」分隔符（D3）。零宽空格也算：它夹在名字前时不改读音，
# 但会破坏 `idx - 1` 的紧邻判断。
_CALLNAME_DELIMS = " ，,：:、\u2005\u00a0\u200b"

# 疑问信号。中文提问常常不带问号（「推荐个咖啡」「今天去哪吃」），只看结尾标点
# 拦不住；这张表注定会被绕过，作用是挡掉大部分噪音，剩下的交给阈值。
QUESTION_MARKS = (
    "？", "?", "吗", "呢", "哪", "什么", "为什么", "怎么", "怎样", "如何",
    "是不是", "有没有", "多久", "多少", "会不会", "能不能", "可不可以",
    "行不行", "是什么", "啥", "咋", "几点", "谁", "多大", "怎么样",
)


def norm(text: str) -> str:
    """把消息洗成「能稳定比较」的形态。

    微信 4.x 在 @ 与昵称之间会塞 U+2005；群消息里还可能混进不换行空格
    （U+00A0）和零宽空格（U+200B）。这些都干扰「@ 我 / 称呼我」的判定，
    先全部去掉；全角 ＠ 一并转成半角 @，后面只认一种写法。
    """
    return _WS_RE.sub("", str(text or "").replace("\uFF20", "@"))


def _is_ws(ch: str) -> bool:
    """是不是空白分隔符。U+200B 不是 `isspace()`，但在这里就该当空白看。"""
    return ch.isspace() or ch == "\u200b"


def _is_punct(ch: str) -> bool:
    """是不是标点（Unicode P 类）。名字 token 到这里就该停。"""
    return unicodedata.category(ch).startswith("P")


def mentions(text: str) -> List[str]:
    """摘出文本里每个 @ 后面的名字 token。

    这里**没有**复用 norm()：norm 会把空格删掉，于是「@deepChat 你好」会粘成
    「@deepChat你好」，名字反而取不出来。所以只做轻量归一：全角 ＠ -> @，
    把各种空白当分隔符（而不是删掉）。token 到空白/标点/@ 为止，长度 1..20。
    """
    raw = str(text or "").replace("\uFF20", "@")
    out: List[str] = []
    for match in re.finditer("@", raw):
        i = match.end()
        while i < len(raw) and _is_ws(raw[i]):
            i += 1
        start = i
        while i < len(raw) and i - start < 20:
            ch = raw[i]
            if ch in ("@", "\uFF20") or _is_ws(ch) or _is_punct(ch):
                break
            i += 1
        if i > start:
            token = raw[start:i]
            if token not in out:
                out.append(token)
    return out


def _strip_leading_at(text: str) -> str:
    """剥掉开头的一个 @token（连同它后面紧邻的空白）。

    支持全角 ＠ 和微信 4.x 塞在 @ 与名字之间的 U+2005。@ 后面没有名字时不剥，
    原样返回——那些字符是内容不是称呼。
    """
    n = len(text)
    i = 0
    while i < n and _is_ws(text[i]):
        i += 1
    if i >= n or text[i] not in ("@", "\uFF20"):
        return text
    at = i
    i += 1
    while i < n and _is_ws(text[i]):
        i += 1
    start = i
    while i < n and i - start < 20:
        ch = text[i]
        if ch in ("@", "\uFF20") or _is_ws(ch) or _is_punct(ch):
            break
        i += 1
    if i == start:
        return text
    end = i
    while end < n and _is_ws(text[end]):
        end += 1
    return text[:at] + text[end:]


def strip_mentions(text: str, names: Iterable[str]) -> str:
    """抹掉开头连续的 @token，用于拼给模型看的正文。

    `names` 参数只用于接口对齐（调用方把自己的一堆名字传进来），这里**不认识也抹**：
    群消息正文前面挂着的 @ 都只是「叫谁」，不是内容，全剥掉更干净。
    抹空返回原文，否则模型会拿到一条空消息。
    """
    raw = str(text or "")
    cleaned = raw
    # 对齐旧 `_strip_at` 的 count=3：开头最多剥三个 @ 前缀，防极端文本死循环。
    for _ in range(3):
        stripped = _strip_leading_at(cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    result = cleaned.strip()
    return result or raw


def _is_ascii_word(name: str) -> bool:
    """纯 ASCII 字母数字才算「ASCII 词」，走词边界规则；其余走中文呼语位置。"""
    return bool(re.fullmatch(r"[A-Za-z0-9]+", name))


def _chinese_hit(text: str, name: str) -> bool:
    """中文词按「呼语位置」判定：前 8 字符内 / 紧跟标点空格 / 紧跟 @。"""
    idx = text.find(name)
    while idx != -1:
        before = text[idx - 1] if idx > 0 else ""
        if idx < 8:
            return True
        if before in _CALLNAME_DELIMS or before in ("@", "\uFF20"):
            return True
        idx = text.find(name, idx + 1)
    return False


def _dedup_names(names: Iterable[str]) -> List[str]:
    """候选名去重（ASCII 大小写不敏感）。"""
    out: List[str] = []
    seen: set = set()
    for raw in names or []:
        name = str(raw or "").strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def callname_hit(text: str, names: Iterable[str]) -> Optional[str]:
    """按 D3 规则判断有没有「叫到机器人」，命中返回那个词，否则 None。

    两套规则不能混用：
    - ASCII 词（deep / deepChat / bot）做词边界：前后不能紧邻字母数字，
      否则「DeepSeek」「deepbot」会被误判成在叫它。
    - 中文词没有可靠的词边界，改成「呼语位置」：「小深，在吗」算叫到，
      「深度学习是什么」不算。
    """
    plain = str(text or "").replace("\uFF20", "@")
    for name in _dedup_names(names):
        if _is_ascii_word(name):
            pattern = re.compile(
                r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", re.IGNORECASE
            )
            if pattern.search(plain):
                return name
        elif _chinese_hit(plain, name):
            return name
    return None


@dataclass
class Verdict:
    """一次判定的结果。`hit=None` 是 legacy 标记（engage.enabled=false 时）。"""

    hit: Optional[bool]
    score: int
    level: str
    reasons: List[str]

    def brief(self) -> str:
        """一行摘要，日志和面板都用它。"""
        if self.hit is None:
            state = "legacy"
        elif self.hit:
            state = "hit"
        else:
            state = "miss"
        return f"score={self.score} {state} reasons={','.join(self.reasons)}"


class TalkState:
    """连续性窗口：回复成功后开窗，窗口内同一个人接着说 +40。

    为什么按 (chat_id, sender) 而不是按 chat_id：群里 A 和 B 都在说话，
    按 chat_id 开窗会把 A 的连续性算到 B 头上。窗口不落盘：90~180 秒的窗口，
    进程重启本身就超过它，落盘只多一处不一致状态。
    """

    def __init__(self, window: float = 90.0, extend: float = 180.0, cap: int = 256) -> None:
        self.window = float(window)
        self.extend = float(extend)
        self.cap = int(cap)
        # key=(chat_id, sender) -> [过期时间, 已连轮数, 最近活动时间]
        self._windows: Dict[Tuple[str, str], List[float]] = {}

    def _now(self, now: Optional[float]) -> float:
        return time.time() if now is None else float(now)

    def open(self, chat_id: str, sender: str, now: Optional[float] = None) -> None:
        """回复成功后开窗 / 续窗。连说满 3 轮把窗口从 90s 延到 180s。"""
        if not chat_id or not sender:
            return
        t = self._now(now)
        key = (str(chat_id), str(sender))
        cur = self._windows.get(key)
        if cur is None or t > cur[0]:
            # 冷启动或上一窗口已过期：重新起一轮
            self._windows[key] = [t + self.window, 1.0, t]
        else:
            rounds = cur[1] + 1.0
            span = self.extend if rounds >= 3 else self.window
            self._windows[key] = [t + span, rounds, t]
        self.prune(t)

    def bonus(self, chat_id: str, sender: str, now: Optional[float] = None) -> int:
        """窗口内这个 (chat_id, sender) 继续说话，返回 40；否则 0。"""
        t = self._now(now)
        cur = self._windows.get((str(chat_id), str(sender)))
        if cur is None or t > cur[0]:
            return 0
        cur[2] = t
        return 40

    def prune(self, now: Optional[float] = None) -> None:
        """一小时无活动清理 + 容量 256 兜底（按最近活动淘汰最旧）。"""
        t = self._now(now)
        stale = [k for k, v in self._windows.items() if t - v[2] > 3600.0]
        for k in stale:
            self._windows.pop(k, None)
        if len(self._windows) > self.cap:
            ordered = sorted(self._windows.items(), key=lambda kv: kv[1][2])
            for k, _ in ordered[: len(self._windows) - self.cap]:
                self._windows.pop(k, None)


def _member_index(members: List[dict]) -> Dict[str, str]:
    """成员表 -> {名字小写: wxid}。同名多人的名字置 ""，不参与「@ 别人」判定。

    同名时没法确定 @ 的到底是哪一个，按「不确定」处理（不给分也不扣分），
    宁可少回一条，也不能把 @ 甲误判成 @ 乙去插嘴。
    """
    first: Dict[str, str] = {}
    ambiguous: set = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        wxid = str(member.get("username") or "")
        for field in ("nick_name", "remark"):
            name = str(member.get(field) or "").strip()
            if not name:
                continue
            key = name.lower()
            if key not in first:
                first[key] = wxid
            elif first[key] != wxid:
                ambiguous.add(key)
    index: Dict[str, str] = {}
    for key, wxid in first.items():
        index[key] = "" if key in ambiguous else wxid
    return index


class MemberCache:
    """群成员表缓存：第一次读库，之后 TTL 内不重复读。

    provider 抛异常或返回空 -> 缓存值记为 None，`names` 返回 None（排除法不启用，
    只认精确匹配）。容量 64 个群，只缓存最近用过的，防止群很多时内存无限涨。
    """

    def __init__(self, provider: Callable[[str], List[dict]], ttl: float = 600.0,
                 cap: int = 64) -> None:
        self.provider = provider
        self.ttl = float(ttl)
        self.cap = int(cap)
        # chat_id -> (抓取时间, 成员列表 | None)
        self._cache: Dict[str, Tuple[float, Optional[List[dict]]]] = {}

    def _fetch(self, chat_id: str) -> Optional[List[dict]]:
        key = str(chat_id)
        hit = self._cache.get(key)
        now = time.time()
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        members: Optional[List[dict]] = None
        try:
            raw = self.provider(key)
            members = list(raw) if raw else []
        except Exception as exc:  # noqa: BLE001
            # 失败也要缓存（记住这次失败），否则每条消息都重试一次读库。
            logger.warning("读群成员失败（%s），排除法本轮不启用：%s", key, exc)
        self._cache[key] = (now, members)
        self._trim(now)
        return members

    def names(self, chat_id: str) -> Optional[Dict[str, str]]:
        """{名字小写: wxid}；provider 异常/返回空 -> None。"""
        members = self._fetch(chat_id)
        if not members:
            return None
        return _member_index(members)

    def self_names(self, chat_id: str, self_wxid: str) -> List[str]:
        """本群成员表里自己那条的 nick_name + remark。"""
        members = self._fetch(chat_id)
        if not members:
            return []
        target = str(self_wxid or "")
        out: List[str] = []
        for member in members:
            if not isinstance(member, dict):
                continue
            if str(member.get("username") or "") != target:
                continue
            for field in ("nick_name", "remark"):
                name = str(member.get(field) or "").strip()
                if name and name not in out:
                    out.append(name)
        return out

    def _trim(self, now: float) -> None:
        if len(self._cache) > self.cap:
            ordered = sorted(self._cache.items(), key=lambda kv: kv[1][0])
            for key, _ in ordered[: len(self._cache) - self.cap]:
                self._cache.pop(key, None)


def _cfg_get(cfg: Any, path: str, default: Any) -> Any:
    """读配置：优先走 cfg.get("a.b.c", default)，裸 dict 也兼容。"""
    if cfg is None:
        return default
    get = getattr(cfg, "get", None)
    if callable(get):
        try:
            return get(path, default)
        except Exception:  # noqa: BLE001
            return default
    node: Any = cfg
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return default if node is None else node


def _is_question(text: str) -> bool:
    plain = norm(text)
    return any(mark in plain for mark in QUESTION_MARKS)


class GroupEngager:
    """群聊接话打分器。纯内存，不持锁、不调网络、不读磁盘。

    判定顺序就是语义（D6）：
      1. blocked -> 不回
      2. 非群 -> 恒回（私聊不受本机制管）
      3. engage.enabled 假 -> 返回 hit=None 的 legacy 标记（由 Bot 走老逻辑）
      4. 归一化文本，算六项信号，累加得分
      5. 按 call_score / join_owner / join_friend 出 hit
      6. 记状态（S3 接 MonitorState，S1 只留口子）
    """

    def __init__(self, cfg: Any, people: Any, talk: Any, members: Any) -> None:
        self.cfg = cfg
        self.people = people
        self.talk = talk
        self.members = members
        self.enabled = bool(_cfg_get(cfg, "group.engage.enabled", True))
        self.call_score = int(_cfg_get(cfg, "group.engage.call_score", 50))
        self.join_owner = int(_cfg_get(cfg, "group.engage.join_owner", 45))
        self.join_friend = int(_cfg_get(cfg, "group.engage.join_friend", 60))
        # 状态记录口子：S3 的 Bot 会把 self.state（MonitorState）挂上来。
        self.state: Any = None

    def judge(
        self,
        *,
        chat_id: str,
        sender: str,
        sender_name: str,
        text: str,
        is_group: bool,
        self_names: Sequence[str],
        self_wxid: str,
        persona_names: Sequence[str],
        now: Optional[float] = None,
    ) -> Verdict:
        level = self.people.level_of(sender) if self.people is not None else "stranger"
        if level == "blocked":
            return Verdict(False, 0, level, ["blocked"])
        if not is_group:
            return Verdict(True, 0, level, ["private"])
        if not self.enabled:
            return Verdict(None, 0, level, ["legacy"])

        reasons: List[str] = []
        score = 0

        # 候选名 = 机器人自己的名字：self_names（账号昵称 + config 别名 + 群内自己的
        # 昵称/备注，由调用方拼好）+ 角色名 + people.json 里给自己那条的 alias。
        candidates = _dedup_names(list(self_names or []) + list(persona_names or []))
        if self.people is not None and self_wxid:
            own_alias = self.people.alias_of(self_wxid)
            if own_alias:
                candidates = _dedup_names(candidates + [own_alias])
        cand_lower = {c.lower() for c in candidates}

        # 1. @ 两段式（D2）：精确命中 +100；未命中但名字不在成员表 +60；
        #    命中成员表里的别人 -50。成员表读不到 -> 排除法不启用，只认精确匹配。
        toks = mentions(text)
        member_index = self.members.names(chat_id) if self.members is not None else None
        at_signal: Optional[str] = None
        has_self = any(t.lower() in cand_lower for t in toks)
        has_other = False
        has_unknown = False
        if member_index is not None:
            for token in toks:
                key = token.lower()
                if key in member_index:
                    if member_index[key] != "":
                        has_other = True
                else:
                    has_unknown = True

        if has_self:
            score += W_AT_ME
            reasons.append("at_me")
            at_signal = "at_me"
        elif member_index is not None and has_other:
            score += W_AT_OTHER
            reasons.append("at_other")
            at_signal = "at_other"
        elif member_index is not None and has_unknown:
            score += W_AT_UNKNOWN
            reasons.append("at_unknown")
            at_signal = "at_unknown"

        # 2. 称呼（D3）
        called = callname_hit(text, candidates)
        if called:
            score += W_CALLNAME
            reasons.append("callname")

        # 3. 连续性（D4）
        if self.talk is not None and self.talk.bonus(chat_id, sender, now) > 0:
            score += W_CONTINUITY
            reasons.append("continuity")

        # 4. 疑问
        body = strip_mentions(text, candidates)
        if _is_question(body):
            score += W_QUESTION
            reasons.append("question")

        # 5. 太短：只在「没被叫到」时扣。被 @ / 被叫名字，回一句「你好」是对的，
        #    不该因为正文短就不理；但没人叫的「哈哈哈」「👍👍」就得拦下来。
        explicitly_called = at_signal in ("at_me", "at_unknown") or called is not None
        if not explicitly_called and len(norm(body)) < MIN_CHARS:
            score += W_TOO_SHORT
            reasons.append("too_short")

        # 6. 阈值出 hit（D6）。
        #    call_score 只给「被叫到」（@ 我 / @ 未知 / 称呼命中）用——它是「叫名字
        #    照旧能叫应」的低门槛。没被叫到时走「主动接话」门槛：owner/friend 才有，
        #    stranger 只能被叫到（join 用 999 表达，这里直接不主动接话）。
        if explicitly_called:
            hit = score >= self.call_score
        elif level in ("owner", "friend"):
            join = self.join_owner if level == "owner" else self.join_friend
            hit = score >= join
        else:
            hit = False

        self._record(hit, reasons)
        return Verdict(hit, score, level, reasons)

    def _record(self, hit: bool, reasons: List[str]) -> None:
        """记状态的口子。S1 不接 MonitorState，接线的 Bot 会把 self.state 挂上来。

        这里用 getattr 而不是直接 import MonitorState：engage 是纯函数模块，
        不该反过来依赖面板层，否则以后把 engage 单独抽出去测还得拖着 control。
        """
        state = getattr(self, "state", None)
        engage = getattr(state, "engage", None) if state is not None else None
        if callable(engage):
            engage(hit, reasons)
