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
from _support import line


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

    line("结论")
    print("  闸门、切分、去重都正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
