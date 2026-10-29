# -*- coding: utf-8 -*-
"""打标（V1 半量版）。

V1 只出三个东西：类型、是否未解决、初始重要度。
主文档里的效价和唤醒度留到 V3 —— 自用聊天用不上那个精度，还多两个要调的参数。

纯规则，不调模型：这一段的价值是「永不失败」，一旦引入模型就失去了稳定性。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple

# 类型 -> 初始重要度（主文档：待办与指令最高、偏好与事实次之、闲聊最低）
IMPORTANCE: dict[str, int] = {
    "待办": 5,
    "指令": 5,
    "事实": 3,
    "偏好": 3,
    "闲聊": 1,
}

TODO_WORDS = ("记得", "别忘了", "提醒", "要交", "得去", "约了", "回头", "明天要",
              "后天要", "待会", "晚点", "别忘")
ORDER_WORDS = ("帮我", "给我", "你去", "查一下", "发一下", "找一下", "看一下",
               "能不能帮", "麻烦你")
PREF_WORDS = ("喜欢", "讨厌", "爱吃", "不爱", "习惯", "最爱", "受不了", "偏爱",
              "不吃", "不喝", "爱喝", "就喝", "只喝", "平时都", "一般都",
              "从来不", "总是", "喝咖啡")
FACT_WORDS = ("我是", "我在", "我住", "我姓", "我今年", "我有", "生日", "我老家",
              "我养", "我在做")
DONE_WORDS = ("搞定了", "完成了", "解决了", "弄完了", "办好了", "已经", "搞完",
              "弄好", "做完", "交完")


@dataclass
class Tags:
    type: str
    unresolved: bool
    importance: int

    def as_dict(self) -> dict:
        return {"type": self.type, "unresolved": self.unresolved,
                "importance": self.importance}


def _hit(text: str, words: Tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def classify(text: str) -> Tags:
    """规则打标。任何输入都返回一组标签，不抛异常。

    Args:
        text: 消息原文。

    Returns:
        Tags: 类型、是否未解决、初始重要度。
    """
    text = (text or "").strip()
    if not text:
        return Tags("闲聊", False, IMPORTANCE["闲聊"])

    # 顺序有意义：待办 > 指令 > 偏好 > 事实 > 闲聊
    if _hit(text, TODO_WORDS):
        kind = "待办"
    elif _hit(text, ORDER_WORDS):
        kind = "指令"
    elif _hit(text, PREF_WORDS):
        kind = "偏好"
    elif _hit(text, FACT_WORDS):
        kind = "事实"
    else:
        kind = "闲聊"

    # 只有「本该有下文」的两类才可能是未解决；闲聊永远算已经了结，
    # 否则库里会堆满「哈哈」这种永远未解决的垃圾。
    unresolved = kind in ("待办", "指令") and not _hit(text, DONE_WORDS)

    return Tags(kind, unresolved, IMPORTANCE[kind])


# ================================================================ 值得存吗
# 不是每句话都配占一个记忆名额。「推荐个咖啡」是让你干活，不是关于你的信息；
# 「嗯」「在吗」连信息都算不上。这些东西存进去只是稀释检索质量。
MIN_CHARS = 5

# 撤回、拍一拍、入群招呼……这些在 4.x 里类型仍可能报「文本」，只能看内容
NOISE_PATTERNS = (
    "撤回了一条消息", "recalled a message", "拍了拍", "拍拍",
    "加入了群聊", "邀请", "移出了群聊", "开启了朋友验证",
    "现在可以开始聊天", "你已添加", "以上是打招呼的内容",
    "消息已发出，但被对方拒收", "开启了消息免打扰",
)

# 中文提问常常不带问号（「推荐个咖啡」「今天天气怎么样」），只看结尾标点拦不住
QUESTION_WORDS = ("什么", "为什么", "怎么", "怎样", "如何", "哪些", "哪个", "哪儿",
                  "哪里", "是不是", "有没有", "多久", "多少", "会不会", "能不能",
                  "可不可以", "行不行", "是什么", "啥", "咋", "几点", "几点了",
                  "谁", "来着", "哪句", "哪个", "多大", "多久了", "还记得",
                  "忘了")
# 注意：中文的问法列举不完，这张表注定会被绕过 —— 它的作用是在不引入模型调用
# 的前提下挡掉大部分噪音，剩下的交给容量淘汰。别指望它完备。
REQUEST_WORDS = ("推荐", "帮我", "给我", "查一查", "查一下", "看一下", "讲讲",
                 "讲一下", "讲一讲", "说说", "介绍", "举个例子", "来一段",
                 "翻译", "总结", "写个", "润色", "改一下", "生成", "搜一下")

# 再长就截断。记忆装的是「一句话的事实」，不是长篇；
# 超长文本会占满上下文预算，也会让相似度被无关细节稀释。
MAX_CHARS = 200

# 这些短语里的「我」是宾语不是主语（「帮我写周报」说的不是用户自己），
# 判「这句话在说谁」之前先抹掉，否则会误判成「关于用户的信息」
REQUEST_WITH_ME = ("帮我", "给我", "替我", "让我", "为我", "告诉我", "提醒我",
                   "向我", "对我", "问我", "跟我讲", "给我讲")

# 命中这些词说明句子里夹带了信息，哪怕看着像提问也不能丢
INFORMATIVE_WORDS = PREF_WORDS + FACT_WORDS + TODO_WORDS + DONE_WORDS


def worth_remembering(text: str) -> bool:
    """这句话值不值得占一个记忆名额。

    核心判据是「这句话在说谁」：主语是自己（带「我」或命中陈述类词）就存，
    否则如果它是在让你干活或是在提问，就不存。

    宁可存错也不能丢真的 —— 所以中性陈述（既没「我」也不像提问，例如
    「早餐吃了包子」）一律放过；只有当句子明确是在向你索取时才拦。

    Args:
        text: 消息原文。

    Returns:
        bool: True 表示写进记忆库。
    """
    t = (text or "").strip()
    if len(t) < MIN_CHARS:
        return False
    if any(p in t for p in NOISE_PATTERNS):
        return False
    # 纯符号、纯表情没有语义
    if not any(ch.isalnum() for ch in t):
        return False

    # 说的到底是谁：先把宾语位置的「我」抹掉，剩下的才是真在讲用户自己
    probe = t
    for word in REQUEST_WITH_ME:
        probe = probe.replace(word, "")

    # 只看第一个标点之前那一段。尾巴挂个「你觉得呢」不影响主体是陈述，
    # 但也正因为这样，「我喜欢美式，你觉得呢」这类才算陈述而不是提问。
    head = re.split(r"[，,。.！!？?；;、\n]", t)[0].strip()
    asking = (_hit(head, QUESTION_WORDS) or _hit(head, REQUEST_WORDS)
              or _hit(head, ORDER_WORDS))
    about_me = "我" in probe or _hit(t, INFORMATIVE_WORDS)

    if not asking:
        return True
    # 句首就在问，多半是在向你索取信息（「我在哪家公司来着」）—— 那不是新事实。
    # 只有偏好、待办、完结这类硬信息才救得回来；「事实」不救，
    # 因为 FACT_WORDS 里的「我在」太宽，会把一切「我在……」都捞回来。
    return about_me and _hit(head, PREF_WORDS + TODO_WORDS + DONE_WORDS)


def clip_for_store(text: str) -> str:
    """太长的话掐到 MAX_CHARS 再入库，尾巴用省略号标出截断。"""
    t = (text or "").strip()
    if len(t) <= MAX_CHARS:
        return t
    return t[:MAX_CHARS].rstrip() + "……"
