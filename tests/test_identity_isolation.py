# -*- coding: utf-8 -*-
"""回归：用户身份与记忆隔离（P0）。

    .venv\\Scripts\\python.exe tests\\test_identity_isolation.py

验的核心只有一句：**别人的记忆，我一条都捞不到**。
每条都写成「反例断言」——不仅验「我查得到自己的」，还要验「我查不到他的」。

隔离维度（见 `方案-用户身份与记忆隔离.md`）：
  owner  = u:<wxid> 某人的 / g:<chat_id> 群公共 / "" 未归属（旧数据，永不参与检索）
  scope  = private / group
  chat_id= 群 id（跨群隔离）

隔离：临时目录，跑完删掉，不碰真实 data/。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot import Bot  # noqa: E402
from app.embeddings import Embedder  # noqa: E402
from app.memory import MemoryStore  # noqa: E402
from app.user_levels import UserLevelStore  # noqa: E402
from app.retrieval import MemoryRetriever  # noqa: E402
from _support import FAILS, check, line  # noqa: E402

A = "u:wxid_aaa"
B = "u:wxid_bbb"
GROUP1 = "111@chatroom"
GROUP2 = "222@chatroom"



def texts(hits) -> str:
    return " | ".join(h.memory.text for h in hits)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="deepchat-test-身份隔离-"))
    try:
        return _run(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(work: Path) -> int:
    print("=" * 52)
    print(" 用户身份与记忆隔离回归（临时目录）")
    print("=" * 52)
    print(f"  数据目录：{work}")

    embedder = Embedder()
    store = MemoryStore(work / "memory", embedder=embedder, capacity=50,
                        judge=lambda new, old: False, snapshot_every=1000)
    retriever = MemoryRetriever(store, embedder=embedder, min_score=0.30, top_k=6)

    line("1. 两个人各说各的，互不串台")
    store.put("叫我老大就行", owner=A, scope="private")
    store.put("叫我小明", owner=B, scope="private")
    a_hits = retriever.search("怎么称呼我", owner=A, scope="private")
    b_hits = retriever.search("怎么称呼我", owner=B, scope="private")
    check("A 能查到自己的称呼", "老大" in texts(a_hits), texts(a_hits) or "空")
    check("★ A 查不到 B 的称呼（反例）", "小明" not in texts(a_hits), texts(a_hits))
    check("B 能查到自己的称呼", "小明" in texts(b_hits), texts(b_hits) or "空")
    check("★ B 查不到 A 的称呼（反例）", "老大" not in texts(b_hits), texts(b_hits))

    line("2. 同一句话两个人说，必须存两条（查重按 owner 分组）")
    store.put("我喜欢喝美式咖啡", owner=A, scope="private")
    store.put("我喜欢喝美式咖啡", owner=B, scope="private")
    same_a = retriever.search("喝什么咖啡", owner=A, scope="private")
    same_b = retriever.search("喝什么咖啡", owner=B, scope="private")
    check("A 有自己那条", "美式" in texts(same_a), texts(same_a))
    check("B 也有自己那条", "美式" in texts(same_b), texts(same_b))
    check("★ 两个人各存一条，没被合并成一条",
          store.active(owner=A, scope="private").__len__() == 2
          and store.active(owner=B, scope="private").__len__() == 2,
          f"A={len(store.active(owner=A))} B={len(store.active(owner=B))}")

    line("3. 旧数据（无归属）永不参与任何人的检索")
    store.put("这条是迁移前的旧记忆", owner="", scope="private")
    old_a = retriever.search("迁移前的旧记忆", owner=A, scope="private")
    check("★ A 捞不到无归属记录（反例）", old_a == [], texts(old_a) or "空")
    check("legacy_count 能报出有多少条待处理", store.legacy_count() == 1,
          str(store.legacy_count()))
    check("不过滤时才看得到（命令行调试路径）",
          any("迁移前" in h.memory.text for h in retriever.search("迁移前的旧记忆")),
          "不过滤 = 不过滤")

    line("4. 场景隔离：私聊记忆不会漏进群里")
    glued = retriever.search("叫我老大", owner=A, scope="group", chat_id=GROUP1)
    check("★ 拿 A 的身份去群里查，查不到私聊那条", glued == [], texts(glued) or "空")

    line("5. 跨群隔离：同一个人的两个群互不相通")
    store.put("我在一群说过下周三出差", owner=A, scope="group", chat_id=GROUP1)
    g1 = retriever.search("我下周去哪", owner=A, scope="group", chat_id=GROUP1)
    g2 = retriever.search("我下周去哪", owner=A, scope="group", chat_id=GROUP2)
    check("一群查得到", "出差" in texts(g1), texts(g1) or "空")
    check("★ 二群查不到（跨群隔离）", g2 == [], texts(g2) or "空")

    line("6. 写入口的防呆")
    raised = ""
    try:
        store.put("群记忆但没给群 id", owner=A, scope="group")
    except ValueError as exc:
        raised = str(exc)
    check("scope=group 没给 chat_id 直接拒写", bool(raised), raised[:30])

    line("7. 身份说明块：称呼只能来自 people.json")
    people = UserLevelStore(work / "people.json", owners=["wxid_boss"],
                         key_users=["wxid_pal"], default="stranger")
    people.set("wxid_pal", level="friend", alias="老张")
    bot = Bot.__new__(Bot)
    bot.people = people
    owner_block = bot._identity_block("wxid_boss", "老板", False)
    friend_block = bot._identity_block("wxid_pal", "老张的微信", True)
    stranger_block = bot._identity_block("wxid_stranger", "路人甲", True)
    check("主人那块带称呼", "主人" in owner_block, owner_block.strip()[:30])
    check("熟人的称呼用 people.json 里的 alias（不是显示名）",
          "老张" in friend_block, friend_block.strip()[:30])
    check("陌生人那块点出名字（模型才知道在跟谁说话）",
          "路人甲" in stranger_block, stranger_block.strip()[:30])
    check("没有 sender 就不加这块（命令行/自检）",
          bot._identity_block("", "", False) == "")

    line("8. 群记忆配额：一个群刷屏不挤别人的私聊记忆")
    # 群记忆和私聊共用一个 capacity。没有单群配额的话，一个热闹的群迟早把
    # 私聊的记忆顶出去 —— 主人反而先失忆，这是最不该发生的一种「失忆」。
    quota = MemoryStore(work / "memory-quota", embedder=embedder, capacity=40,
                        group_quota=3, judge=lambda new, old: False,
                        snapshot_every=1000)
    quota.put("我的生日是三月十七号", owner=A, scope="private")
    for one in ("明天上午十点评审", "下午的会改到四点", "这周五要交周报",
                "楼下咖啡店关门了", "我先撤了明天聊", "记得带电脑"):
        quota.put(one, owner=A, scope="group", chat_id=GROUP1)
    check("一群只留到配额（3 条）",
          len(quota.active(scope="group", chat_id=GROUP1)) == 3,
          str(len(quota.active(scope="group", chat_id=GROUP1))))
    check("★ 私聊那条一条没少（群刷屏没把它顶掉）",
          len(quota.active(owner=A, scope="private")) == 1,
          str(len(quota.active(owner=A, scope="private"))))
    quota.put("二群说的一句话", owner=A, scope="group", chat_id=GROUP2)
    check("别的群不被连坐",
          len(quota.active(scope="group", chat_id=GROUP2)) == 1,
          str(len(quota.active(scope="group", chat_id=GROUP2))))

    line("")
    if FAILS:
        print(f" 共 {len(FAILS)} 项失败：")
        for name in FAILS:
            print("   - " + name)
        return 1
    print(" 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
