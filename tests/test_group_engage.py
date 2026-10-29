# -*- coding: utf-8 -*-
"""回归：群聊接话打分（S1）。

    .venv\\Scripts\\python.exe tests\\test_group_engage.py

把 §8.1 的 20 项验收逐条落成断言，再补边界（空文本 / 纯 @ / 同名多人 / 全角＠ /
超长 @token）。核心一句话：**被叫到就回、熟人能接两轮、别人聊天不插嘴**。

隔离：等级库与成员表都造在临时目录 / 内存里，跑完删掉，不碰真实 data/。
不联网、不调模型、不起微信。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.group_engage import (  # noqa: E402
    GroupEngager,
    MemberCache,
    TalkState,
    Verdict,
    callname_hit,
    mentions,
    norm,
    strip_mentions,
)
from app.user_levels import UserLevelStore  # noqa: E402
from _support import FAILS, check, line  # noqa: E402

GROUP = "12345678@chatroom"
SELF = "wxid_self"
OWNER = "wxid_owner"
FRIEND = "wxid_friend"
STRANGER = "wxid_stranger"



class Cfg:
    """够用的配置壳：只实现 get("a.b.c", default)。"""

    def __init__(self, data: dict) -> None:
        self.data = data

    def get(self, path: str, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _members() -> list[dict]:
    """一个固定群：自己 + 老张。账号昵称 deepChat、备注小深。"""
    return [
        {"username": SELF, "nick_name": "deepChat", "remark": "小深"},
        {"username": "wxid_laozhang", "nick_name": "老张", "remark": ""},
    ]


def _provider(fail: bool = False, empty: bool = False, members=None):
    def provider(chat_id: str):
        if fail:
            raise RuntimeError("读库失败")
        if empty:
            return []
        return list(members if members is not None else _members())

    return provider


def _engager(work: Path, cfg: dict | None = None, provider=None,
             people=None) -> tuple[GroupEngager, UserLevelStore, TalkState]:
    cfg = cfg or {"group": {"engage": {"enabled": True}}}
    people = people or UserLevelStore(work / "people.json", default="stranger")
    talk = TalkState()
    members = MemberCache(provider or _provider())
    return GroupEngager(Cfg(cfg), people, talk, members), people, talk


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="deepchat-test-接话-"))
    try:
        return _run(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(work: Path) -> int:
    print("=" * 52)
    print(" 群聊接话打分回归（临时目录）")
    print("=" * 52)
    print(f"  数据目录：{work}")

    _part_norm()
    _part_mentions()
    _part_strip()
    _part_callname()
    _part_verdict()
    _part_talk()
    _part_members(work)
    _part_judge(work)

    line("")
    if FAILS:
        print(f" 共 {len(FAILS)} 项失败：")
        for name in FAILS:
            print("   - " + name)
        return 1
    print(" 全部通过")
    return 0


# ---------------------------------------------------------------- 1. 归一化
def _part_norm() -> None:
    line("1. norm：去空白 / 全角＠")
    check("U+2005 与空格被去掉", norm("@\u2005deepChat 你好") == "@deepChat你好",
          repr(norm("@\u2005deepChat 你好")))
    check("全角＠转半角", norm("＠deepChat") == "@deepChat", repr(norm("＠deepChat")))
    check("U+00A0 / U+200B 被去掉", norm("a\u00a0b\u200bc") == "abc",
          repr(norm("a\u00a0b\u200bc")))
    check("普通空白全去掉", norm("  deep  chat  ") == "deepchat", repr(norm("  deep  chat  ")))


# ---------------------------------------------------------------- 2. @ 提取
def _part_mentions() -> None:
    line("2. mentions：@ 后面的名字 token")
    check("@账号昵称", mentions("@deepChat 你好") == ["deepChat"], str(mentions("@deepChat 你好")))
    check("@ 与名字之间夹 U+2005", mentions("@\u2005deepChat 你好") == ["deepChat"],
          str(mentions("@\u2005deepChat 你好")))
    check("@中文名", mentions("@老张 你去看看") == ["老张"], str(mentions("@老张 你去看看")))
    check("超 20 字符的 token 截断", mentions("@" + "a" * 25 + " 你好") == ["a" * 20],
          str(len(mentions("@" + "a" * 25 + " 你好")[0])))
    check("纯 @ 没有名字", mentions("@@@") == [], str(mentions("@@@")))


# ---------------------------------------------------------------- 3. 抹 @ 前缀
def _part_strip() -> None:
    line("3. strip_mentions：抹开头连续 @")
    check("单个 @ 前缀抹掉", strip_mentions("@deepChat 你好", ["deepChat"]) == "你好",
          strip_mentions("@deepChat 你好", ["deepChat"]))
    check("连续两个 @ 前缀都抹", strip_mentions("@deepChat @老张 你好", ["deepChat"]) == "你好",
          strip_mentions("@deepChat @老张 你好", ["deepChat"]))
    check("抹空返回原文", strip_mentions("@deepChat", ["deepChat"]) == "@deepChat",
          strip_mentions("@deepChat", ["deepChat"]))
    check("全角＠ + U+2005 也能抹", strip_mentions("＠\u2005deepChat 你好", ["deepChat"]) == "你好",
          strip_mentions("＠\u2005deepChat 你好", ["deepChat"]))


# ---------------------------------------------------------------- 4. 称呼
def _part_callname() -> None:
    line("4. callname_hit：D3 两套规则")
    check("中文呼语位置（前 8 字符）", callname_hit("小深，在吗", ["小深"]) == "小深")
    check("★ 中文非呼语位置不命中（深度学习）", callname_hit("深度学习是什么", ["小深"]) is None)
    check("ASCII 词边界命中", callname_hit("deep 你觉得呢", ["deep"]) == "deep")
    check("★ DeepSeek 不命中 deep", callname_hit("DeepSeek 好用吗", ["deep"]) is None)
    check("★ deepbot 不命中 deep", callname_hit("deepbot", ["deep"]) is None)
    check("逗号后的中文名也算呼语位置", callname_hit("在吗，小深", ["小深"]) == "小深")


# ---------------------------------------------------------------- 5. Verdict
def _part_verdict() -> None:
    line("5. Verdict.brief")
    check("hit 摘要格式", Verdict(True, 80, "friend", ["callname", "question"]).brief()
          == "score=80 hit reasons=callname,question")
    check("miss 摘要格式", Verdict(False, 40, "stranger", ["continuity"]).brief()
          == "score=40 miss reasons=continuity")


# ---------------------------------------------------------------- 6. 连续性窗口
def _part_talk() -> None:
    line("6. TalkState：连续性窗口")
    talk = TalkState()
    talk.open(GROUP, OWNER, now=0)
    check("开窗后窗口内 +40", talk.bonus(GROUP, OWNER, now=10) == 40)
    check("★ 别人说话不享受这 40", talk.bonus(GROUP, STRANGER, now=10) == 0)

    talk2 = TalkState()
    talk2.open(GROUP, OWNER, now=0)
    check("超过 90 秒无连续性分", talk2.bonus(GROUP, OWNER, now=100) == 0)

    talk3 = TalkState()
    talk3.open(GROUP, OWNER, now=0)
    talk3.open(GROUP, OWNER, now=10)
    talk3.open(GROUP, OWNER, now=20)
    check("连说 3 轮后窗口延到 180s（90s 时已过期，180s 仍有效）",
          talk3.bonus(GROUP, OWNER, now=150) == 40)

    talk4 = TalkState(cap=3)
    for i in range(5):
        talk4.open(GROUP, f"wxid_{i}", now=i)
    check("容量 cap 兜底（最多 3 条）", len(talk4._windows) == 3, str(len(talk4._windows)))

    talk5 = TalkState()
    talk5.open(GROUP, OWNER, now=0)
    talk5.prune(now=4000)
    check("一小时无活动清理", talk5.bonus(GROUP, OWNER, now=4000) == 0)


# ---------------------------------------------------------------- 7. 成员表缓存
def _part_members(work: Path) -> None:
    line("7. MemberCache：成员表缓存")
    cache = MemberCache(_provider())
    names = cache.names(GROUP)
    check("名字 -> wxid 索引", names == {"deepchat": SELF, "小深": SELF, "老张": "wxid_laozhang"},
          str(names))
    check("self_names 取自己那条", cache.self_names(GROUP, SELF) == ["deepChat", "小深"],
          str(cache.self_names(GROUP, SELF)))

    check("provider 返回空 -> None（排除法不启用）",
          MemberCache(_provider(empty=True)).names(GROUP) is None)
    check("provider 抛异常 -> None 且不抛",
          MemberCache(_provider(fail=True)).names(GROUP) is None)

    dup = MemberCache(_provider(members=[
        {"username": "a", "nick_name": "老王", "remark": ""},
        {"username": "b", "nick_name": "老王", "remark": ""},
    ]))
    check("同昵称多人 -> 名字置空（歧义不参与判定）", dup.names(GROUP) == {"老王": ""},
          str(dup.names(GROUP)))

    calls = {"n": 0}

    def counting(chat_id: str):
        calls["n"] += 1
        return _members()

    ttl_cache = MemberCache(counting)
    ttl_cache.names(GROUP)
    ttl_cache.names(GROUP)
    check("TTL 内不重复读库", calls["n"] == 1, str(calls["n"]))


# ---------------------------------------------------------------- 8. 判定（§8.1 20 项）
def _part_judge(work: Path) -> None:
    line("8. GroupEngager.judge：20 项验收")

    eng, people, talk = _engager(work, provider=_provider())
    # 「deep」也在候选里：验收 10 明确把它当称呼词（config 里可手配别名）。
    self_names = ["deepChat", "小深", "deep"]

    def judge(text: str, sender: str = STRANGER, *, engager=None, **kw):
        e = engager or eng
        return e.judge(
            chat_id=GROUP, sender=sender, sender_name=sender, text=text,
            is_group=True, self_names=self_names, self_wxid=SELF,
            persona_names=[], **kw,
        )

    # 2 @账号昵称
    v = judge("@deepChat 你好")
    check("@账号昵称 -> at_me 且 score>=100", v.hit and "at_me" in v.reasons and v.score >= 100,
          v.brief())

    # 3 @群昵称（不在成员表）
    v = judge("@群昵称 你好")
    check("@群昵称不在成员表 -> at_unknown score=60 回",
          v.hit and "at_unknown" in v.reasons and v.score == 60, v.brief())

    # 4 @老张（在成员表）
    v = judge("@老张 你去看看")
    check("@老张在成员表 -> at_other score<=0 不回",
          (not v.hit) and "at_other" in v.reasons and v.score <= 0, v.brief())

    # 5/6 成员表读取失败
    eng_fail, _, _ = _engager(work / "fail", provider=_provider(fail=True))
    v = judge("@deepChat", engager=eng_fail)
    check("成员表失败仍走精确匹配（@deepChat 回）", v.hit and "at_me" in v.reasons, v.brief())
    v = judge("@某未知名字", engager=eng_fail)
    check("成员表失败 + 未知名字 -> 不回", not v.hit, v.brief())

    # 7 DeepSeek 含 deep
    v = judge("DeepSeek 好用吗")
    check("★ DeepSeek 不命中称呼、陌生人不回",
          (not v.hit) and "callname" not in v.reasons, v.brief())

    # 8 深度学习含 深（候选是小深）
    v = judge("深度学习是什么")
    check("★ 深度学习不命中称呼、不回",
          (not v.hit) and "callname" not in v.reasons, v.brief())

    # 9 小深，在吗
    v = judge("小深，在吗")
    check("小深命中称呼回", v.hit and "callname" in v.reasons, v.brief())

    # 10 deep 你觉得呢
    v = judge("deep 你觉得呢")
    check("deep 称呼50+疑问30=80 回", v.hit and v.score == 80, v.brief())

    # 11 owner 连续对话（开窗后「今天去哪吃」）
    people.set(OWNER, level="owner", alias="老板")
    talk.open(GROUP, OWNER, now=0)
    v = judge("今天去哪吃", sender=OWNER, now=10)
    check("owner 第二句 40+30=70 回",
          v.hit and v.score == 70 and "continuity" in v.reasons and "question" in v.reasons,
          v.brief())

    # 12 别人没有连续性分
    v = judge("今天去哪吃", sender=STRANGER, now=10)
    check("★ 别人无连续性分、30<999 不回", (not v.hit) and "continuity" not in v.reasons,
          v.brief())

    # 12b 陌生人即便开了窗，连续+疑问也不主动接（40+30=70 < 999）。
    #     这条是 D6 的关键反例：call_score 只给「被叫到」用，主动接话门槛只认 owner/friend。
    eng_sw, _, t_sw = _engager(work / "sw", provider=_provider(), people=people)
    t_sw.open(GROUP, STRANGER, now=0)
    v = eng_sw.judge(chat_id=GROUP, sender=STRANGER, sender_name=STRANGER,
                     text="那这个呢", is_group=True, self_names=self_names,
                     self_wxid=SELF, persona_names=[], now=10)
    check("★ 陌生人开窗后 40+30=70 仍不主动接（999）", (not v.hit) and v.score == 70,
          v.brief())

    # 13 窗口过期（注入 now 超 90s）
    talk2 = TalkState()
    eng2, people2, _ = _engager(work / "expire", provider=_provider(), people=people)
    eng2.talk = talk2
    people2.set(OWNER, level="owner")
    talk2.open(GROUP, OWNER, now=0)
    v = eng2.judge(chat_id=GROUP, sender=OWNER, sender_name=OWNER, text="今天去哪吃",
                   is_group=True, self_names=self_names, self_wxid=SELF,
                   persona_names=[], now=100)
    check("窗口过期 -> 无连续性分", "continuity" not in v.reasons, v.brief())

    # 15 发送失败窗口不打开（没开窗就没有 40）
    eng_no_open, _, _ = _engager(work / "noopen", provider=_provider(), people=people)
    v = eng_no_open.judge(chat_id=GROUP, sender=OWNER, sender_name=OWNER,
                          text="今天去哪吃", is_group=True, self_names=self_names,
                          self_wxid=SELF, persona_names=[], now=0)
    check("没开窗（发送失败）-> 无连续性分", "continuity" not in v.reasons, v.brief())

    # 16 blocked
    people.set("wxid_bad", level="blocked")
    v = judge("在吗", sender="wxid_bad")
    check("blocked 直接 False 且只记 blocked",
          (not v.hit) and v.reasons == ["blocked"] and v.score == 0, v.brief())

    # 17 太短
    v = judge("哈哈哈")
    check("「哈哈哈」too_short 不回", (not v.hit) and "too_short" in v.reasons, v.brief())
    v = judge("👍👍")
    check("「👍👍」too_short 不回", (not v.hit) and "too_short" in v.reasons, v.brief())

    # 18 enabled=false -> legacy
    eng_off, _, _ = _engager(work / "off", cfg={"group": {"engage": {"enabled": False}}},
                             provider=_provider())
    v = judge("deep 在吗", engager=eng_off)
    check("engage.enabled=false -> legacy 标记", v.hit is None and "legacy" in v.reasons,
          v.brief())

    # 19 阈值边界 50 / 49
    eng50, _, _ = _engager(work / "b50", cfg={"group": {"engage": {"call_score": 50}}},
                           provider=_provider())
    eng51, _, _ = _engager(work / "b51", cfg={"group": {"engage": {"call_score": 51}}},
                           provider=_provider())
    v50 = judge("小深", engager=eng50)
    v51 = judge("小深", engager=eng51)
    check("score=50 且 call_score=50 -> 回", v50.hit and v50.score == 50, v50.brief())
    check("score=50 但 call_score=51 -> 不回（即 49 不回的边界）",
          (not v51.hit) and v51.score == 50, v51.brief())

    # 20 非群
    v = eng.judge(chat_id=STRANGER, sender=STRANGER, sender_name=STRANGER,
                  text="随便说什么", is_group=False, self_names=self_names,
                  self_wxid=SELF, persona_names=[])
    check("非群恒回、不计分", v.hit and v.reasons == ["private"] and v.score == 0, v.brief())

    # ---- 额外边界 ----
    v = judge("")
    check("空文本不回", not v.hit, v.brief())
    v = judge("＠deepChat 你好")
    check("全角＠也能 at_me", v.hit and "at_me" in v.reasons, v.brief())

    dup_provider = _provider(members=[
        {"username": "a", "nick_name": "老王", "remark": ""},
        {"username": "b", "nick_name": "老王", "remark": ""},
    ])
    eng_dup, _, _ = _engager(work / "dup", provider=dup_provider)
    v = judge("@老王 你好", engager=eng_dup)
    check("同名多人 -> 歧义，@ 不给分也不扣分",
          (not v.hit) and "at_other" not in v.reasons and "at_unknown" not in v.reasons,
          v.brief())


if __name__ == "__main__":
    sys.exit(main())
