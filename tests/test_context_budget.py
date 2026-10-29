# -*- coding: utf-8 -*-
"""回归：闸门、切分、去重（阶段 4）。

    .venv\\Scripts\\python.exe tests\\test_context_budget.py

不调模型、不接微信，纯本地跑，几秒钟出结果。
看三件事：塞爆了会不会崩、砍的顺序对不对、重复消息会不会回两遍。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot import _Seen
from app.context_budget import ContextBudget
from app.memory import Memory
from app.retrieval import Hit
from app.wechat.reply_split import clean_for_wechat, split_reply
from _support import FAILS, check, line


def fake_hit(index: int, score: float, text: str) -> Hit:
    mem = Memory(id=f"m{index}", text=text, type="事实", importance=3,
                 unresolved=False, created_at=0.0)
    return Hit(memory=mem, similarity=score, score=score)


def main() -> int:
    # ---------------------------------------------------------------- 1
    line("1. 塞 50 轮历史 + 30 条记忆（故意远超预算）")
    gate = ContextBudget(context_limit=2000, reply_reserve=400, keep_turns=2)
    turns = []
    for i in range(50):
        turns.append({"role": "user", "content": f"第{i}轮用户说的话，这里故意写长一点好占位置。" * 3})
        turns.append({"role": "assistant", "content": f"第{i}轮回复，同样写长一点用来撑爆预算。" * 3})
    hits = [fake_hit(i, 0.9 - i * 0.01, f"记忆内容第{i}条：" + "细节" * 20) for i in range(30)]

    built = gate.build("你是一个聊天的人。", turns, hits)
    stats = built.stats
    print(f"  上下文 {stats['tokens']} token / 预算 {stats['budget']}")
    print(f"  对话留 {stats['turns']} 条，记忆留 {stats['memories']} 条"
          f"（砍掉 {stats['memories_dropped']} 条记忆、{stats['turns_trimmed']} 条对话）")
    assert stats["tokens"] <= stats["budget"], "超预算了，闸门失效"
    assert stats["turns"] >= 4, "最近两轮没保住"
    print("  -> 没有超预算，最近两轮对话保住了")

    # ---------------------------------------------------------------- 2
    line("2. 完全没有记忆时，上下文里不该出现记忆段落")
    empty = gate.build("你是一个聊天的人。", turns[:4], [])
    has_header = "你记得的事" in empty.messages[0]["content"]
    print(f"  记忆段落：{'出现了（不对）' if has_header else '没有（正确）'}")
    assert not has_header

    # ---------------------------------------------------------------- 3
    line("3. 长回复切分")
    long_text = ("今天这天气是真的热，出门五分钟就一身汗。"
                 "我下午在楼下买了个冰美式，坐了一会儿才缓过来。"
                 "你要是也出来的话，记得带把伞，太阳太毒了。")
    cleaned = clean_for_wechat(long_text)
    bubbles = split_reply(cleaned, max_chars=40, max_bubbles=3)
    for i, b in enumerate(bubbles, 1):
        print(f"  第{i}条（{len(b)}字）：{b}")
    assert 1 <= len(bubbles) <= 3 and all(bubbles)
    print(f"  -> 切成 {len(bubbles)} 条，没有空条，没超过上限")

    # ---------------------------------------------------------------- 4
    line("4. 同一条消息进来两次")
    with tempfile.TemporaryDirectory() as tmp:
        seen = _Seen(Path(tmp) / "seen.json")
        first = seen.add("wxid_abc|local_id|1001")
        second = seen.add("wxid_abc|local_id|1001")
        other = seen.add("wxid_abc|local_id|1002")
        print(f"  第一次：{'收下' if first else '跳过'}")
        print(f"  同一条再来：{'收下（不对）' if second else '跳过（正确，不会回两遍）'}")
        print(f"  新的一条：{'收下' if other else '跳过'}")
        assert first and not second and other

        # 重启后仍然记得
        again = _Seen(Path(tmp) / "seen.json")
        print(f"  重启后再来同一条：{'收下（不对）' if again.add('wxid_abc|local_id|1001') else '跳过（正确）'}")
        assert not again.add("wxid_abc|local_id|1001")

        # 落盘格式：一行一条追加，不再每次整体重写 5000 条（约 200KB）。
        # 老文件是 JSON 数组，升级后必须还能读——读不出来的后果是「重启把老消息全再回一遍」。
        print("\n  落盘格式（追加 / 兼容老数组 / 半截行）")
        legacy = Path(tmp) / "seen_legacy.json"
        legacy.write_text('["old|1", "old|2"]', encoding="utf-8")
        upgraded = _Seen(legacy)
        check("老格式（JSON 数组）还能读进来", not upgraded.add("old|1"),
              "读不出来的话这里会返回 True")
        upgraded.add("new|3")
        first_line = legacy.read_text(encoding="utf-8").splitlines()[0]
        check("★ 写过一次就转成新格式（不再是整体重写的数组）",
              not first_line.lstrip().startswith("["), first_line[:40])
        check("转格式之后老指纹还在（不会因为升级而重答）",
              not upgraded.add("old|2"))

        replay = _Seen(legacy)
        check("追加格式能回放（重开仍然认得刚写的）",
              not replay.add("new|3") and not replay.add("old|2"))

        broken = Path(tmp) / "seen_broken.json"
        broken.write_text('"a|1"\n"b|2"\n"c|没写完', encoding="utf-8")
        salvaged = _Seen(broken)
        check("★ 被强杀留下的半截行不拖垮整个指纹库（其余照常生效）",
              not salvaged.add("a|1") and not salvaged.add("b|2"))
        check("半截行之后仍然能继续写入", salvaged.add("d|4"))

    line("结论")
    if FAILS:
        # 以前这里无条件 return 0：`check()` 记下的失败只是打印出来，
        # 退出码仍然是 0，run_all 会把「有断言红了」当成通过。
        # 用断言（assert）的旧用例会抛异常，反而没这个问题；加了 check 之后必须自己收口。
        print(f"  有 {len(FAILS)} 项没过：" + "、".join(FAILS))
        return 1
    print("  闸门、切分、去重都正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
