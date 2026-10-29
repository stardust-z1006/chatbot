# -*- coding: utf-8 -*-
"""记忆存储。

V1 口径（刻意和主文档不同，见实现方案）：
  - 常驻内存 + 追加日志 + 定期快照，不用数据库。400 条的规模内存足够。
  - 容量满了**淘汰**最没用的，不做四级降级。降级会留下「只剩关键词还占位」
    的中间态，淘汰干净得多，也没有摘要要生成。
  - 查重只和有效记录比。更新时旧条作废、留档、不进检索——这样「改口」可回溯，
    又不会被旧说法干扰。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from .embeddings import Embedder, cosine
from .message_tagging import classify

logger = logging.getLogger(__name__)

# 查重阈值。低于下限算新事，高于上限算同一条，中间灰区才值得问模型。
NEW_BELOW = 0.80
SAME_ABOVE = 0.96

ACTIVE, VOID, DROPPED = "active", "void", "dropped"

# 作废记录（改口留下的旧说法）只留最近这么多条。
# 留档的意义是「最近的改口能回溯」，半年前那句说法没人查；不封顶的话，
# 每一次改口、每一次面板指派都留一条（还带 512 维向量），
# 快照和内存会单调增长，而 capacity 只管 active，统计上看不出来。
VOID_KEEP = 200

# touch 缓存的条数上限。攒够就写一次，别一条一次 fsync。
TOUCH_BUFFER = 64


@dataclass
class Memory:
    """一条记忆。字段都是 V1 真正用得上的，没有为将来预留。"""

    id: str
    text: str
    type: str
    importance: int
    unresolved: bool
    created_at: float
    last_used_at: float = 0.0
    use_count: int = 0
    status: str = ACTIVE
    vector: Optional[List[float]] = None
    note: str = ""
    superseded_by: str = ""
    # 同一条记忆的另一种说法。换说法时不丢旧文本，检索两边都算，取高的那边。
    alias_text: str = ""
    alias_vector: Optional[List[float]] = None
    # ---- 归属（2026-10-27 加）。不写这三个字段，记忆库就是所有人共读共写的大锅：
    #      A 说一句「叫我老大」，B 进来检索照样命中，于是对谁都喊老大。
    #      取值约定：u:<wxid> 某人的 / g:<chat_id> 群公共 / "" 未归属（旧数据，永不参与检索）。
    owner: str = ""
    scope: str = "private"   # private / group
    chat_id: str = ""        # 仅 scope=group 时用，负责「跨群隔离」

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class MemoryStore:
    """记忆的读写门面。所有落盘都在这里，别处不许直接碰文件。"""

    def __init__(
        self,
        data_dir: Path,
        embedder: Optional[Embedder] = None,
        capacity: int = 400,
        judge: Optional[Callable[[str, str], bool]] = None,
        snapshot_every: int = 100,
        group_quota: int = 80,
        void_keep: int = VOID_KEEP,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.data_dir / "memory.log"
        self.snap_path = self.data_dir / "memory.snapshot.json"
        self.embedder = embedder or Embedder()
        self.capacity = max(10, int(capacity))
        # 群聊记忆的**单群**配额。0 = 不限（不推荐：一个热闹的群迟早把私聊的记忆挤出去）。
        # 它和 capacity 不是一回事：capacity 是全局兜底，这个是「这个群最多占多少」。
        self.group_quota = max(0, int(group_quota))
        self.judge = judge
        self.snapshot_every = max(10, int(snapshot_every))
        self.void_keep = max(0, int(void_keep))

        self._items: Dict[str, Memory] = {}
        self._appends = 0
        # 待落盘的 touch 行。检索命中时不该每条都 fsync（一次回复最多命中 6 条，
        # 就是 6 次 open+write+fsync），攒起来由 flush_touches() 一次性写。
        self._touch_buf: List[str] = []
        # 所有会话的 Timer 线程和面板线程都会碰这个库，而「读一遍全部记忆」
        # 和「写一条」同时发生就是 dictionary changed size during iteration。
        # 一把可重入锁足够：读写都很快（400 条），没有争抢的价值。
        self._lock = threading.RLock()
        self.load()

    # ------------------------------------------------------------ 载入
    def load(self) -> None:
        """快照 + 回放日志。任何一行坏掉就跳过并计数，不为一行坏数据放弃整库。"""
        self._items.clear()
        broken = 0

        if self.snap_path.exists():
            try:
                raw = json.loads(self.snap_path.read_text(encoding="utf-8"))
                for item in raw.get("records", []):
                    mem = Memory(**item)
                    self._items[mem.id] = mem
            except Exception as exc:
                logger.warning("快照损坏，按空库启动：%s", exc)

        if self.log_path.exists():
            with self.log_path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        op = json.loads(line)
                    except Exception:
                        broken += 1
                        continue
                    # 两种行：整条记录（add/update/void/drop）和精简的 touch。
                    # touch 只记 id + 计数，回放时要能找到已载入的那条。
                    if "mem" in op:
                        try:
                            mem = Memory(**op["mem"])
                        except Exception:
                            broken += 1
                            continue
                        self._items[mem.id] = mem
                    elif op.get("op") == "touch":
                        target = self._items.get(str(op.get("id") or ""))
                        if target is not None:
                            target.use_count = int(
                                op.get("use_count") or target.use_count)
                            target.last_used_at = float(
                                op.get("last_used_at") or target.last_used_at)
                    else:
                        broken += 1

        if broken:
            logger.warning("记忆日志有 %d 行损坏，已跳过", broken)
        logger.info("记忆库就绪：%d 条（有效 %d 条）", len(self._items), len(self.active()))

    # ------------------------------------------------------------ 查询
    def active(self, owner: Optional[str] = None, scope: Optional[str] = None,
               chat_id: Optional[str] = None) -> List[Memory]:
        """当前有效的记忆，可按归属过滤。

        Args:
            owner: 只返回这个归属的。**传 "" 时只返回未归属的旧记录**（用来做迁移/统计），
                传 None 表示不过滤。差别很重要：新人千万别把 None 当成「空」。
            scope: private / group；None 不过滤。
            chat_id: 群隔离用；None 不过滤。

        Returns:
            List[Memory]: 列表副本，调用方随便遍历。
        """
        with self._lock:
            out = []
            for mem in self._items.values():
                if mem.status != ACTIVE:
                    continue
                if owner is not None and mem.owner != owner:
                    continue
                if scope is not None and mem.scope != scope:
                    continue
                if chat_id is not None and mem.chat_id != chat_id:
                    continue
                out.append(mem)
            return out

    def legacy_count(self) -> int:
        """没有归属的旧记录条数。面板/日志用它提示「还有多少条需要处理」。"""
        with self._lock:
            return sum(1 for m in self._items.values()
                       if m.status == ACTIVE and not m.owner)

    def owner_counts(self) -> Dict[str, int]:
        """当前有效记忆按 owner 汇总，给面板的「归属」下拉用。

        key 里会有 `""`（迁移前的旧数据，谁都不属于）。这里**不做过滤**：
        面板要把它单独渲染成「未归属（N）」并且排在最下面，那是「待补救」的意思，
        和「某个人的」不是同一类选项 —— 过滤掉就没人知道还有多少条在失忆。
        """
        with self._lock:
            counts: Dict[str, int] = {}
            for mem in self._items.values():
                if mem.status != ACTIVE:
                    continue
                counts[mem.owner] = counts.get(mem.owner, 0) + 1
            return counts

    def get(self, memory_id: str) -> Optional[Memory]:
        with self._lock:
            return self._items.get(memory_id)

    def __iter__(self) -> Iterator[Memory]:
        return iter(self.active())

    def stats(self) -> Dict[str, int]:
        with self._lock:
            active = [m for m in self._items.values() if m.status == ACTIVE]
            return {
                "total": len(self._items),
                "active": len(active),
                "void": sum(1 for m in self._items.values() if m.status == VOID),
                "dropped": sum(1 for m in self._items.values() if m.status == DROPPED),
                "capacity": self.capacity,
            }

    # ------------------------------------------------------------ 写入
    def put(self, text: str, tags: Optional[Dict[str, Any]] = None,
            owner: str = "", scope: str = "private", chat_id: str = "") -> Dict[str, Any]:
        """写入一条。返回 {"action": added|updated|duplicated, "memory": Memory}。

        查重三步：和**同一个 owner** 的有效记录算相似度 -> 高于上限直接更新、低于下限直接新增、
        中间灰区交给 judge（没配 judge 就先新增并打标，留给 V3 整理时合并）。

        Args:
            owner: 归属（`u:<wxid>` / `g:<chat_id>` / `""` 未归属）。**查重只在同一 owner 内做**，
                否则 A 和 B 说同一句话会被合并成一条 —— 那等于隔离在写入层就失效了。
            scope: private / group。
            chat_id: 群 id（scope=group 时必填，负责跨群隔离）。

        Raises:
            ValueError: scope=group 却没给 chat_id（宁可拒写，也不留半截记录）。

        锁的边界是刻意划开的：编码和「问裁判」都不持锁。裁判一次最长 12 秒，
        持着锁问它等于让所有会话的记忆读写一起卡住 —— 而这把锁本来是防
        「读一半被写穿」的，不是拿来串行化网络调用的。
        """
        text = (text or "").strip()
        if not text:
            return {"action": "duplicated", "memory": None}
        scope = str(scope or "private")
        if scope == "group" and not str(chat_id or "").strip():
            raise ValueError("scope=group 必须给 chat_id（跨群隔离靠它）")

        vec = self.embedder.encode_one(text)

        with self._lock:
            best, best_sim = self._best_match(vec, text, owner)
            candidate = best
            action = ""
            if candidate is None or best_sim <= NEW_BELOW:
                action = "add"
            elif best_sim >= SAME_ABOVE:
                action = "update"

        # ---- 灰区：锁外问一次模型「是不是同一件事」
        same = False
        if action == "" and candidate is not None and self.judge is not None:
            same = bool(self.judge(text, candidate.text))
            action = "update" if same else "add"
        elif action == "":
            action = "add"

        with self._lock:
            live = self._items.get(candidate.id) if candidate is not None else None
            if action == "update" and live is not None and live.status == ACTIVE:
                new = self._make(text, tags, vec, owner, scope, chat_id)
                self._inherit(new, live)
                # 顺序要紧：先把新说法写进日志，再作废旧条。
                # 反过来的话，写盘失败会留下「旧的已作废、新的没落地」——
                # 那就真丢了一条记忆；现在最坏是新旧并存，下次查重再合并。
                self._append("update", new)
                self._void(live.id, new.id,
                           reason="被更新的说法取代" if best_sim >= SAME_ABOVE
                           else "模型判定同一件事")
                self._after_write()
                return {"action": "updated", "memory": new,
                        "replaced": live, "similarity": best_sim}

            mem = self._make(text, tags, vec, owner, scope, chat_id)
            if candidate is not None and best_sim > NEW_BELOW:
                # 灰区但没合并：留个记号，V3 整理时合并
                mem.note = f"疑似与 {candidate.id} 重复（相似度 {best_sim:.2f}）"
            self._append("add", mem)
            self._after_write()
            return {"action": "added", "memory": mem, "similarity": best_sim}

    def touch(self, memory_id: str, now: Optional[float] = None) -> None:
        """被检索用上就记一笔。V1 只记事实，不动强度——反馈闭环留到 V3。"""
        with self._lock:
            mem = self._items.get(memory_id)
            if mem is None:
                return
            mem.use_count += 1
            mem.last_used_at = now or time.time()
            self._append_touch(mem)

    def void(self, memory_id: str, reason: str = "手工作废") -> bool:
        with self._lock:
            mem = self._items.get(memory_id)
            if mem is None or mem.status != ACTIVE:
                return False
            self._void(mem.id, "", reason)
            return True

    def assign(self, memory_id: str, owner: str, scope: str = "",
               chat_id: str = "") -> Optional[Memory]:
        """把一条记忆改到某人/某个群名下。面板「指派」按钮的落点。

        为什么是「新建 + 作废旧条」，不是就地改 `owner` 字段：本库的语义是
        追加日志 + 定期快照，`_items` 里的对象和磁盘上的行必须始终能对上。
        就地改的话磁盘上那一行还是 `owner=""`，重启回放出来又变成未归属——
        面板上点一百次都白点。所以照抄 `put` 里「改口」的做法：新记录先落盘，
        旧记录再作废（顺序见 `_void` 的说明）。

        为什么**不走查重**：`_best_match` 是按 owner 分组的，而这条记忆在目标
        owner 名下本来就是「第一次出现」，查重没有意义；更糟的是如果目标名下
        已经有一句很像的话，查重会把两条合并、把旧条作废掉——用户记忆就真的
        少了一条。指派是「换个主人」，不是「改口」。

        Args:
            memory_id: 要指派的记忆 id。
            owner: 目标归属，`u:<wxid>` 或 `g:<chat_id>`。空串不收：指派的目的
                就是「让它有主」，撤销归属请用 `void`（那是留档，语义清楚得多）。
            scope: 留空表示沿用旧记录的 scope。
            chat_id: scope=group 时必填，负责跨群隔离。

        Returns:
            Optional[Memory]: 新记录。找不到 id、或那条已经不在 active（作废/淘汰）
            时返回 None —— 调用方按「这条没了」处理即可，不必捕获异常。

        Raises:
            ValueError: owner 格式不对，或 scope=group 却没给 chat_id。
                和 `put` 里「scope=group 必须给 chat_id」是同一个口径：宁可拒写，
                也不留一条谁都查不到的半截记录。
        """
        owner = str(owner or "").strip()
        prefix, sep, tail = owner.partition(":")
        if not sep or prefix not in ("u", "g") or not tail.strip():
            raise ValueError("归属要写成 u:<wxid> 或 g:<群id>（没有归属的记忆谁都查不到）")

        with self._lock:
            old = self._items.get(str(memory_id or "").strip())
            # 作废/淘汰的也当「这条没了」：作废是留档不复活，指派一条作废记录
            # 等于把它从坟里拉回来，和面板上「作废」的承诺冲突。
            if old is None or old.status != ACTIVE:
                return None

            new_scope = str(scope or old.scope or "private")
            new_chat = str(chat_id or "").strip() or old.chat_id
            if new_scope == "group" and not new_chat:
                raise ValueError("scope=group 必须给 chat_id（跨群隔离靠它）")

            # type/importance/unresolved 原样带过去：这些是分类算出来的结论，
            # 重新 `classify` 一遍只会因为换了个 owner 就降级（闲聊→被挤掉）。
            tags = {
                "type": old.type,
                "importance": old.importance,
                "unresolved": old.unresolved,
            }
            new = self._make(old.text, tags, old.vector, owner, new_scope, new_chat)
            # 旧说法也带着走：留 alias 是为了「换过说法的那条」被指派之后，
            # 用旧说法仍然检索得到（见 _inherit 里那个 0.654 → 0.459 的坑）。
            new.alias_text = old.alias_text
            new.alias_vector = old.alias_vector

            self._append("update", new)
            self._void(old.id, new.id, reason="面板指派归属")
            self._after_write()
            logger.info("记忆指派归属：%s → %s（%s）", old.id, owner, old.text[:20])
            return new

    # ------------------------------------------------------------ 内部
    def _make(self, text: str, tags: Optional[Dict[str, Any]], vec: Optional[List[float]],
              owner: str = "", scope: str = "private", chat_id: str = "") -> Memory:
        now = time.time()
        if tags is None:
            t = classify(text)
            tags = t.as_dict()
        return Memory(
            id=_new_id(),
            text=text,
            type=str(tags.get("type", "闲聊")),
            importance=int(tags.get("importance", 1)),
            unresolved=bool(tags.get("unresolved", False)),
            created_at=now,
            last_used_at=0.0,
            use_count=0,
            status=ACTIVE,
            vector=vec,
            owner=owner,
            scope=scope,
            chat_id=chat_id if scope == "group" else "",
        )

    def _inherit(self, new: Memory, old: Memory) -> None:
        """同一条记忆换说法时：重要度取高、旧说法留作别名。

        实测过两个坑，都出在「换说法」上：
          1. 「我喜欢喝美式咖啡」判成偏好（3），换成「我平时就喝美式」规则判成
             闲聊（1），容量一满就被当垃圾淘汰 -> 重要度以旧条为准取高值。
          2. 两句话长度差不多，但新说法里没了「咖啡」二字，与「咖啡」的相似度
             从 0.654 掉到 0.459，直接被门槛拦掉。
             光留新文本不行、光留旧文本也不行（用户可能就是想改口）——
             所以两条都留着，检索时取相似度高的那一边。
        """
        if new.importance < old.importance:
            new.importance = old.importance
        if new.type == "闲聊" and old.type != "闲聊":
            new.type = old.type
        new.unresolved = new.unresolved or old.unresolved

        if old.text and old.text != new.text:
            new.alias_text = old.text
            new.alias_vector = old.vector

    def _void(self, old_id: str, new_id: str, reason: str) -> None:
        """作废一条。状态变化本身也要进日志，否则重启后旧条会复活。

        做法是「先造副本 → 落盘 → 再替换内存」，不是就地改字段：
        就地改的话，日志写失败时内存已经是 VOID，磁盘上是 ACTIVE，
        重启后新旧两种说法会同时有效。
        """
        old = self._items.get(old_id)
        if old is None:
            return
        retired = replace(old, status=VOID, superseded_by=new_id, note=reason)
        self._append("void", retired)

    def _best_match(self, vec: Optional[List[float]], text: str, owner: str = ""):
        """找最像的有效记录。没有向量就退字面重合，保证查重不瞎。"""
        best: Optional[Memory] = None
        best_sim = 0.0
        for mem in self.active(owner=owner):
            if vec is not None and mem.vector is not None:
                sim = cosine(vec, mem.vector)
                if mem.alias_vector is not None:
                    sim = max(sim, cosine(vec, mem.alias_vector))
            elif text.strip() == mem.text.strip() or (
                    mem.alias_text and text.strip() == mem.alias_text.strip()):
                # 一字不差就是同一句。二元组重合度打完 0.9 的折之后只有 0.9，
                # 够不到 SAME_ABOVE(0.96)，会导致「原话重说一遍」被当成新事
                # ——向量不可用时查重就废了。所以这一档单独置 1.0。
                sim = 1.0
            else:
                sim = _text_overlap(text, mem.text) * 0.9  # 字面相似天然偏高，压一点
            if sim > best_sim:
                best, best_sim = mem, sim
        return best, best_sim

    def _after_write(self) -> None:
        self._evict()
        if self._appends >= self.snapshot_every:
            self.snapshot()

    def _evict(self) -> None:
        """容量满了淘汰最没用的：重要度低、且最久没被用。

        先按「单群配额」收群记忆，再按全局容量兜底。顺序不能反：
        反过来群记忆可能先把全局名额占满、把私聊记忆挤掉，私聊主人反而先失忆。
        """
        with self._lock:
            if self.group_quota:
                self._evict_groups()
            while len(self.active()) > self.capacity:
                weakest = min(
                    self.active(),
                    key=lambda m: (m.importance, m.last_used_at or m.created_at),
                )
                # 同样先落盘再改内存：DROPPED 也要能从日志回放出来
                self._append("drop", replace(weakest, status=DROPPED, note="容量满，淘汰"))
                logger.info("淘汰记忆：%s（重要度 %d）", weakest.text[:20], weakest.importance)

    def _evict_groups(self) -> None:
        """群记忆按**单个群**收配额。

        只在超配额的那个群里淘汰——A 群刷屏不该让 B 群背锅，也不该动私聊的记忆。
        淘汰口径和全局一致（重要度低、最久没用），不引入新参数。
        """
        by_chat: Dict[str, List[Memory]] = {}
        for mem in self.active(scope="group"):
            by_chat.setdefault(mem.chat_id, []).append(mem)
        for chat_id, items in by_chat.items():
            over = len(items) - self.group_quota
            if over <= 0:
                continue
            items.sort(key=lambda m: (m.importance, m.last_used_at or m.created_at))
            for weakest in items[:over]:
                self._append("drop", replace(
                    weakest, status=DROPPED,
                    note=f"群记忆超配额（每群 {self.group_quota} 条），淘汰"))
            logger.info("群记忆超配额，淘汰 %d 条（%s）", over, chat_id)

    def enforce_capacity(self) -> int:
        """按当前 capacity 立刻收一遍，返回淘汰了几条。

        面板把容量调小时要马上生效——否则得等下一次写入才收，
        中间这段时间「容量 50 实际存着 300 条」，看数字会以为设置没生效。
        """
        with self._lock:
            before = len(self.active())
            self._evict()
            dropped = before - len(self.active())
            if dropped:
                logger.info("容量收紧，淘汰了 %d 条", dropped)
            return dropped

    def _append(self, op_name: str, mem: Memory) -> None:
        """先落盘再承认成功。写不进去就要抛，不能假装存了。

        原来的顺序是先改内存、后写文件：写失败时内存里已经有这条记忆，
        这一轮能检索到、重启后消失——是最难查的那一类不一致。
        现在只有 fsync 真的返回成功，才把它交给内存。
        """
        line = json.dumps({"op": op_name, "mem": mem.as_dict()}, ensure_ascii=False)
        self._write_line(line)
        self._items[mem.id] = mem
        self._appends += 1

    def _append_touch(self, mem: Memory) -> None:
        """touch 只写「谁、用了几次、什么时候」，不重复写整条记忆。

        一条记忆带 512 维向量（序列化后约 11KB），一次检索最多命中 6 条，
        按整条追加就是 6 次 11KB 的 fsync —— 磁盘瓶颈是自己造出来的。
        回放时靠 id 找回已载入的那条，见 load()。

        这里只是**放进缓冲区**：真正的写盘在 flush_touches()。代价是进程被强杀
        时可能丢最近几十条 touch —— 它们只影响淘汰排序的先后，不影响记忆本身，
        属于可以接受的降级（记忆的增删改仍然是每次 fsync 的）。
        """
        self._touch_buf.append(json.dumps(
            {"op": "touch", "id": mem.id,
             "use_count": mem.use_count, "last_used_at": mem.last_used_at},
            ensure_ascii=False,
        ))
        if len(self._touch_buf) >= TOUCH_BUFFER:
            self.flush_touches()

    def flush_touches(self) -> int:
        """把缓冲的 touch 行一次性写下去，返回写了几条。

        正常调用点是「一轮回复结束」（Bot.handle 末尾）和快照/退出之前。
        写失败只记日志不抛：丢的是「这条被用过」的计数，不该让回复流程挂掉。
        """
        with self._lock:
            if not self._touch_buf:
                return 0
            lines, self._touch_buf = self._touch_buf, []
            try:
                with self.log_path.open("a", encoding="utf-8") as fp:
                    for line in lines:
                        fp.write(line + "\n")
                    fp.flush()
                    os.fsync(fp.fileno())
            except OSError as exc:
                logger.warning("touch 落盘失败，丢了 %d 条使用记录：%s", len(lines), exc)
                return 0
            self._appends += len(lines)
            return len(lines)

    def _write_line(self, line: str) -> None:
        """追加一行并 fsync，两种日志行共用同一套落盘语义。"""
        with self.log_path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
            fp.flush()
            os.fsync(fp.fileno())

    def snapshot(self) -> None:
        """写完整快照后截断日志。先写临时文件再整体替换，杜绝写一半断电。

        整个动作必须在锁里做完：快照内容和「日志截断」如果被一次并发写入
        插在中间，那一行就会跟着旧日志一起消失。
        """
        # 先把缓冲的 touch 落下去：日志马上要被截断，没写的那几条会跟着一起没了。
        self.flush_touches()
        with self._lock:
            keep_void = self._recent_void_ids()
            kept = {
                mem_id: mem for mem_id, mem in self._items.items()
                if mem.status != DROPPED
                and (mem.status != VOID or mem_id in keep_void)
            }
            dropped_void = len(self._items) - len(kept)
            alive = [m.as_dict() for m in kept.values()]
            payload = {"version": 1, "saved_at": time.time(), "records": alive}
            tmp = self.snap_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.snap_path)
            self.log_path.write_text("", encoding="utf-8")
            self._items = kept
            self._appends = 0
            logger.info("快照完成：%d 条%s", len(self._items),
                        f"（另有 {dropped_void} 条久远作废记录不再留档）" if dropped_void else "")

    def _recent_void_ids(self) -> set:
        """要保留的作废记录 id：按创建时间取最近的 `void_keep` 条。"""
        void = [m for m in self._items.values() if m.status == VOID]
        if len(void) <= self.void_keep:
            return {m.id for m in void}
        newest = sorted(void, key=lambda m: m.created_at, reverse=True)[: self.void_keep]
        return {m.id for m in newest}


def _text_overlap(a: str, b: str) -> float:
    """字面重合度（字符二元组 Jaccard）。向量不可用时的兜底，比 nothing 强。"""
    def grams(s: str):
        s = "".join(ch for ch in s if ch.strip())
        if len(s) < 2:
            return {s} if s else set()
        return {s[i:i + 2] for i in range(len(s) - 1)}

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)
