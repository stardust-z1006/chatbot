# -*- coding: utf-8 -*-
"""回归：记忆与检索（阶段 2 + 3）。

    .venv\\Scripts\\python.exe tests\\test_memory.py

验四件事，**每条都是断言，不再只打印**：
  1. 存进去的话，换个说法能捞出来（「咖啡」-> 「我喜欢喝美式」）
  2. 库里没有的事，检索为空（不凑数）
  3. 同一句话再存一次不会变两条；换个说法会合并、旧说法留成别名
  4. 容量满了淘汰最没用的，淘汰后快照重启仍然一致

两条隔离约定（改之前这两个坑都踩过）：
  - 数据写在系统临时目录，跑完删干净，**不碰 data/ 里的真实记忆库**；
  - 裁判用本地假函数，不调模型：真裁判要联网、要钱、结果还不确定，
    拿它做回归等于把「测试通不通过」交给网络。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.embeddings import Embedder  # noqa: E402
from app.memory import MemoryStore  # noqa: E402
from app.retrieval import MemoryRetriever  # noqa: E402
from _support import FAILS, check, line  # noqa: E402

SEED = [
    "我喜欢喝美式咖啡，不加糖",
    "上周跟老张约了周三看球",
    "我讨厌下雨天出门",
    "明天要交季度报告",
    "我家猫叫团子，三岁了",
]

QUERIES = [
    ("推荐个咖啡", "美式"),
    ("我和老张约了啥", "周三"),
]

# 八条互不相干的闲聊，用来撑爆容量（内容太像会被查重吃掉，测不出淘汰）
NOISE = [
    "今天地铁上人特别多",
    "楼下新开了一家面馆",
    "手机膜又摔裂了",
    "会议室空调太冷",
    "同事说他要去云南旅游",
    "快递放在门卫那儿了",
    "周末想把房间收拾一下",
    "这周的电费比上个月高",
    "楼下便利店涨价了",
    "电脑风扇有点响",
    "好久没打篮球了",
    "地铁口的早餐摊换人了",
]



def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="deepchat-test-记忆-"))
    try:
        return _run(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(work: Path) -> int:
    print("=" * 52)
    print(" 记忆与检索回归（临时目录）")
    print("=" * 52)
    print(f"  数据目录：{work}")

    embedder = Embedder()
    # 假裁判：本地恒定判「同一件事」。真裁判要联网，回归不能依赖它。
    judge = lambda new, old: True  # noqa: E731
    store = MemoryStore(work, embedder=embedder, capacity=10,
                        judge=judge, snapshot_every=1000)
    retriever = MemoryRetriever(store, embedder=embedder, min_score=0.55)

    line("1. 写入五条")
    for text in SEED:
        result = store.put(text)
        check(f"写入「{text[:12]}…」", result["action"] == "added", result["action"])
    check("有效条数 = 5", store.stats()["active"] == 5, str(store.stats()))

    line("2. 换个说法能捞到吗")
    for query, expect in QUERIES:
        hits = retriever.search(query, top_k=3)
        text = " ".join(h.memory.text for h in hits)
        check(f"「{query}」捞到「{expect}」", expect in text,
              "、".join(h.brief() for h in hits) or "空")

    line("3. 库里没有的事要空")
    empty = retriever.search("量子力学的测不准原理是什么", top_k=3)
    check("无关问题检索为空（不凑数）", empty == [], f"{len(empty)} 条")

    line("4. 查重：原话再存、换说法再存")
    before = store.stats()["active"]
    same = store.put("我喜欢喝美式咖啡，不加糖")
    check("原话再存 -> updated", same["action"] == "updated", same["action"])
    check("原话再存不增加条数", store.stats()["active"] == before,
          f"{before} -> {store.stats()['active']}")

    other = store.put("我平时就喝美式，不加糖")
    check("换说法 -> updated（裁判判同一件事）", other["action"] == "updated",
          other["action"])
    check("旧说法留成别名", other["memory"].alias_text == "我喜欢喝美式咖啡，不加糖",
          other["memory"].alias_text or "（空）")
    check("作废条数 = 2", store.stats()["void"] == 2, str(store.stats()))

    line("5. 容量满了淘汰谁（容量 10）")
    for text in NOISE:
        store.put(text)
    stats = store.stats()
    check("有效条数被压到容量内", stats["active"] <= 10, str(stats))
    check("确实发生了淘汰", stats["dropped"] > 0, str(stats))

    line("6. 重启后还在吗")
    store.snapshot()
    reloaded = MemoryStore(work, embedder=embedder, capacity=10)
    check("重载后有效条数一致", reloaded.stats()["active"] == stats["active"],
          f"{reloaded.stats()['active']} vs {stats['active']}")
    check("重载后作废条数一致", reloaded.stats()["void"] == stats["void"],
          f"{reloaded.stats()['void']} vs {stats['void']}")
    again = MemoryRetriever(reloaded, embedder=embedder).search("咖啡", top_k=2)
    check("重启后「咖啡」仍能命中", bool(again),
          "、".join(h.brief() for h in again) or "空")

    print()
    if FAILS:
        print(f"有 {len(FAILS)} 项没过：" + "、".join(FAILS))
        return 1
    print("记忆与检索可用：写入、检索、门槛、查重、别名、淘汰、重载全过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
