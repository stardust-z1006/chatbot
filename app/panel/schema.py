# -*- coding: utf-8 -*-
"""后台面板能调的参数清单（唯一来源）。

为什么不自动扫 config.yaml：面板需要知道每一项的**人话名字、取值范围、
改了要不要重启**。这些信息在 yaml 里没有，自动扫只能渲出一堆裸输入框。

每个字段的规则：
  - group：对应面板左侧的一个分区（chat / llm / memory / search / kb /
    history / voice / log / system）
  - kind：bool / int / float / str / enum / list
  - 范围（minimum/maximum）一律硬校验，面板只能这么改，绕不过去
  - restart=True 表示「进程里的对象已按旧值建好」，改了要重启才真正生效
  - hint 是一句「为什么」或「注意什么」，显示在输入框下面
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Field:
    path: str
    label: str
    kind: str
    group: str
    hint: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Tuple[str, ...] = ()
    restart: bool = False
    step: Optional[float] = None
    suffix: str = ""

    def as_dict(self, current: Any, default: Any, overridden: bool) -> Dict[str, Any]:
        return {
            "path": self.path,
            "label": self.label,
            "kind": self.kind,
            "group": self.group,
            "hint": self.hint,
            "min": self.minimum,
            "max": self.maximum,
            "choices": list(self.choices),
            "step": self.step,
            "restart": self.restart,
            "suffix": self.suffix,
            "value": current,
            "default": default,
            "overridden": overridden,
        }


# 左侧分区（顺序即显示顺序）。group 为空的纯展示区，不渲染参数表。
GROUPS: Tuple[str, ...] = (
    "chat",
    "people",
    "llm",
    "memory",
    "search",
    "kb",
    "history",
    "voice",
    "log",
    "system",
)

FIELDS: Tuple[Field, ...] = (
    # ---------------------------------------------------------------- 对话
    Field("wechat.merge_window", "合并窗口", "float", "chat",
          "连着发来好几条时，攒这么久再合并成一句回。给 0 就是一条一回。",
          0.0, 10.0, step=0.1, suffix="秒"),
    Field("wechat.max_age_minutes", "只回最近的消息", "float", "chat",
          "防止启动时把昨晚的旧消息补答一遍。0 = 不限（不建议）。",
          0.0, 1440.0, step=1, suffix="分钟"),
    Field("wechat.group_at_only", "群里只被 @ 才回", "bool", "chat",
          "这是「没单独设过的群」的默认策略。单个群在「会话监视」里可以单独改。"),
    Field("wechat.reply_private", "私聊默认要回", "bool", "chat",
          "关掉就默认不理私聊（单个好友仍可单独设）。话多的人可以先关掉总开关再逐个放开。"),
    Field("wechat.group_keywords", "群聊关键词", "list", "chat",
          "逗号分隔。命中这些词也会回，和 @ 是或的关系。"),
    Field("wechat.bubble_chars", "单条气泡字数", "int", "chat",
          "长回复按这个长度切成多条，像人在打字。", 10, 1000, suffix="字"),
    Field("wechat.max_bubbles", "最多几条气泡", "int", "chat",
          "超出的内容并到最后一条，避免刷屏。", 1, 10, suffix="条"),
    Field("wechat.split_reply", "长回复切分发送", "bool", "chat",
          "关掉就整段一次发出（会显得不像人）。"),
    Field("wechat.typing_per_char", "打字间隔", "float", "chat",
          "气泡之间的停顿，按字数算。给 0 就是一口气全发。",
          0.0, 0.2, step=0.005, suffix="秒/字"),
    Field("wechat.cooldown", "发送冷却", "float", "chat",
          "两条消息之间至少隔这么久，防止连发抢焦点。",
          0.0, 10.0, step=0.1, suffix="秒"),
    Field("wechat.send_dedupe_seconds", "重复发送拦截", "float", "chat",
          "0 = 关。拦的是同一次发送调用内的重复点击（库内部的自动重试），"
          "不是「N 秒内不许发同样的话」——按时间窗拦会把一条回复切出来的相同气泡吞掉。"
          "数值只限定台账保留时长。",
          0.0, 120.0, step=1, suffix="秒"),
    Field("wechat.poll_interval", "轮询间隔", "float", "chat",
          "监听器多久查一次本地消息库。改了要重启才生效。",
          0.2, 10.0, restart=True, step=0.1, suffix="秒"),
    Field("wechat.listen", "只监听这些会话", "list", "chat",
          "留空 = 监听全部会话并自动发现新的。填昵称或 wxid，逗号分隔。改了要重启。",
          restart=True),

    # ---------------------------------------------------------------- 用户等级
    Field("people.default_level", "默认等级", "enum", "people",
          "没单独设过的人算哪一级。陌生人只在被 @ 或被叫到名字时回。",
          choices=("owner", "friend", "stranger")),
    Field("people.owners", "主人的 wxid", "list", "people",
          "填 wxid 或昵称，逗号分隔。这些人按「主人」对待——比在面板里逐个设等级更省事。"),
    Field("people.self_names", "机器人别名", "list", "people",
          "群昵称被改过时手动补，逗号分隔。接话判定会把它们当「叫自己」。"),
    Field("group.engage.enabled", "启用接话打分", "bool", "people",
          "关掉就完全退回「只有 @ 或关键词才回」的旧判定。"),
    Field("group.engage.call_score", "被叫到分数线", "int", "people",
          "@ 或叫名字达到这个分就回。", 10, 200),
    Field("group.engage.join_owner", "主人接话分数线", "int", "people",
          "主人不 @ 也回的门槛，越低越主动。", 0, 200),
    Field("group.engage.join_friend", "熟人接话分数线", "int", "people",
          "熟人主动接话门槛。", 0, 200),
    Field("group.key_users", "关键用户（记群消息）", "list", "chat",
          "填 wxid 或昵称，逗号分隔。这些人在群里说过的话会记进 data/group_log。"),
    Field("group.log_enabled", "记录关键用户群消息", "bool", "chat",
          "关掉就不写 data/group_log。记录只追加、不参与检索，也不影响回不回。"),
    Field("group.log_days", "群消息记录保留天数", "int", "chat",
          "超过这个天数的日文件在启动时删掉。", 1, 365, suffix="天"),

    # ---------------------------------------------------------------- 模型
    Field("llm.model", "模型", "str", "llm",
          "OpenAI 兼容接口的模型名，改完下一句就生效。"),
    Field("llm.temperature", "温度", "float", "llm",
          "越高越放飞，聊天建议 0.8 上下。", 0.0, 1.5, step=0.05),
    Field("llm.max_tokens", "单次回复上限", "int", "llm",
          "太小会被「思考」吃掉预算、回空；不够会自动放大重试。", 128, 4000, suffix="token"),
    Field("llm.timeout", "请求超时", "float", "llm", "", 5, 180, suffix="秒"),
    Field("llm.retries", "失败重试次数", "int", "llm",
          "网络抖动的兜底，给 2 一般够。", 0, 5, suffix="次"),
    Field("llm.tool_rounds", "最多几轮工具调用", "int", "llm",
          "一次回复里模型最多连续查几次。给多了容易在「再查一下」里绕圈。", 1, 8, suffix="轮"),
    Field("llm.tool_budget", "工具结果预算", "int", "llm",
          "多轮工具结果累加的上限，超了当场截断并注明「已截断」。",
          500, 20000, step=100, suffix="token"),

    # ---------------------------------------------------------------- 记忆
    Field("memory.capacity", "记忆容量", "int", "memory",
          "满了淘汰最没用的，不降级。改完立刻按新容量收一遍。", 50, 5000, suffix="条"),
    Field("memory.group_quota", "单群记忆上限", "int", "memory",
          "群里最多存几条。跟记忆容量共用名额，不单独限的话一个热闹的群会把私聊的记忆挤掉。"
          "0 = 不限。", 0, 400, suffix="条"),
    Field("memory.top_k", "每次捞几条", "int", "memory",
          "进上下文的记忆条数上限。", 1, 12, suffix="条"),
    Field("memory.min_score", "证据门槛", "float", "memory",
          "低于这个分数一律不进上下文，宁可少捞也别硬凑。",
          0.0, 1.0, step=0.05),
    Field("memory.weight_similarity", "相似度权重", "float", "memory",
          "排序分 = 相似度 × 这个权重 + 重要度 × 另一个权重。",
          0.0, 1.0, step=0.05),
    Field("memory.weight_importance", "重要度权重", "float", "memory",
          "", 0.0, 1.0, step=0.05),
    Field("memory.longterm", "允许模型主动记", "bool", "memory",
          "关掉就不给模型 remember 工具（自动入库照旧）。"),

    # ---------------------------------------------------------------- 联网
    Field("search.enabled", "启用联网", "bool", "search",
          "关掉就没有 web_search / read_url / weather。"),
    Field("search.max_results", "搜索结果条数", "int", "search", "", 1, 10, suffix="条"),
    Field("search.fetch_pages", "抓几篇正文", "int", "search",
          "0 = 只看摘要（快，但容易只看到标题党）。", 0, 3, suffix="篇"),
    Field("search.page_chars", "正文截取字数", "int", "search", "", 500, 8000,
          step=100, suffix="字"),
    Field("search.timeout", "抓取超时", "float", "search", "", 3, 30, suffix="秒"),

    # ---------------------------------------------------------------- 资料库
    Field("kb.enabled", "启用资料库", "bool", "kb",
          "关掉就没有 kb_search 工具。"),
    Field("kb.top_k", "每次取几段", "int", "kb", "", 1, 8, suffix="段"),
    Field("kb.dirs", "扫描目录", "list", "kb",
          "逗号分隔。改完要重建索引（右边有按钮）。"),
    Field("kb.exts", "文件类型", "list", "kb",
          "逗号分隔，带点。改完要重建索引。"),

    # ---------------------------------------------------------------- 聊天记录
    Field("history.enabled", "启用聊天记录工具", "bool", "history",
          "只读本地消息库，不会给任何人发消息。"),
    Field("history.days", "默认往回翻几天", "int", "history", "", 1, 60, suffix="天"),

    # ---------------------------------------------------------------- 语音
    Field("voice.enabled", "启用语音", "bool", "voice",
          "微信 4.x 发不了语音气泡，发的是 mp3 文件。"),
    Field("voice.mode", "语音模式", "enum", "voice",
          "tool = 模型想发才发；always = 每条文字都配一条语音。",
          choices=("tool", "always")),
    Field("voice.voice", "音色", "str", "voice",
          "edge-tts 音色名，如 zh-CN-XiaoxiaoNeural / zh-CN-YunxiNeural / zh-CN-liaoning-XiaobeiNeural。"),
    Field("voice.rate", "语速", "str", "voice", "带符号的百分比，如 +8% / -5%。"),
    Field("voice.pitch", "音调", "str", "voice", "如 +0Hz / +2Hz。"),
    Field("voice.max_chars", "speak 能念多长", "int", "voice",
          "超过就让模型改发文字。", 10, 200, suffix="字"),
    Field("voice.max_per_turn", "每条回复最多几条语音", "int", "voice", "", 1, 3, suffix="条"),
    Field("voice.tts_max_chars", "合成上限", "int", "voice",
          "长文本先截断再合成，别念一大段。", 20, 1000, suffix="字"),

    # ---------------------------------------------------------------- 日志
    Field("log.level", "日志级别", "enum", "log",
          "改完立刻生效。排查问题开 DEBUG，平时 INFO。",
          choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    Field("log.wechat_debug", "微信接入层调试日志", "bool", "log",
          "发不出消息时再开，会把驱动界面的每一步都写进 bot.log。改了要重启。",
          restart=True),

    # ---------------------------------------------------------------- 系统
    Field("monitor.enabled", "启用后台面板", "bool", "system",
          "改了要重启。关掉后 run.py run 不再起面板。", restart=True),
    Field("monitor.port", "面板端口", "int", "system",
          "改了要重启。默认 8787。", 1024, 65535, restart=True),
)

BY_PATH: Dict[str, Field] = {f.path: f for f in FIELDS}


class FieldError(ValueError):
    """参数不合法。message 直接拿去给面板显示。"""


def coerce(path: str, raw: Any) -> Any:
    """把面板传来的原始值转成正确类型并校验范围。

    Raises:
        FieldError: 路径不存在、类型不对、超范围。
    """
    spec = BY_PATH.get(path)
    if spec is None:
        raise FieldError(f"没有这个参数：{path}")

    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "on", "yes", "开"):
            return True
        if text in ("false", "0", "off", "no", "关"):
            return False
        raise FieldError("只能是开或关")

    if spec.kind in ("int", "float"):
        try:
            value = int(raw) if spec.kind == "int" else float(raw)
        except (TypeError, ValueError):
            raise FieldError("必须是数字") from None
        if spec.minimum is not None and value < spec.minimum:
            raise FieldError(f"不能小于 {_fmt(spec.minimum)}")
        if spec.maximum is not None and value > spec.maximum:
            raise FieldError(f"不能大于 {_fmt(spec.maximum)}")
        return value

    if spec.kind == "enum":
        text = str(raw).strip()
        if text not in spec.choices:
            raise FieldError("只能选 " + " / ".join(spec.choices))
        return text

    if spec.kind == "list":
        if isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw]
        else:
            text = str(raw).replace("，", ",")
            items = [x.strip() for x in text.split(",")]
        return [x for x in items if x]

    text = str(raw).strip()
    if not text:
        raise FieldError("不能为空")
    return text


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def describe(cfg: Any) -> List[Dict[str, Any]]:
    """把当前值、默认值、是否被覆盖一起交给面板。"""
    out: List[Dict[str, Any]] = []
    for spec in FIELDS:
        try:
            current = cfg.get(spec.path)
        except Exception:
            current = None
        out.append(
            spec.as_dict(
                current=current,
                default=cfg.default(spec.path),
                overridden=cfg.is_overridden(spec.path),
            )
        )
    return out


def groups() -> List[str]:
    return list(GROUPS)


def count_in(group: str) -> int:
    return sum(1 for f in FIELDS if f.group == group)
