# -*- coding: utf-8 -*-
"""后台监控的 HTTP 接口。

只监听 127.0.0.1，没有登录：这是本机的运维面板，安全边界就是「能开这台电脑」。
**千万别把 host 改成 0.0.0.0 暴露到公网**——面板能改配置、能重建索引。

和机器人同进程跑（uvicorn 起在后台线程），这样改参数是直接改内存里的
Settings，不用做进程间通信；面板挂了也不影响收消息（异常都在各自线程里）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import schema
from .page import PAGE
from ..personas import PersonaError

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()

# 「未归属」在下拉里的取值。为什么不用空串：空串在这套接口里已经表示
# 「不做过滤」（老面板、自检脚本都这么调），两个含义挤在一个值上必然出事。
# 下拉里那项的 value 就是这个哨兵值，服务端认得，页面也原样拼进查询串。
LEGACY_OWNER = "__legacy__"



def create_app(cfg: Any, bot: Any = None, state: Any = None) -> FastAPI:
    """组装 FastAPI 应用。

    Args:
        cfg: Settings 实例（必须是活的、Bot 正在用的那一个）。
        bot: Bot 实例；None 表示只开面板改配置（机器人没跑）。
        state: MonitorState；None 时用临时的一个（页面能开，但没有运行数据）。
    """
    from .state import MonitorState

    state = state or MonitorState()
    app = FastAPI(title="deepChat · 后台", docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.bot = bot
    app.state.mon = state

    # ------------------------------------------------------------ 错误统一成 JSON
    @app.exception_handler(schema.FieldError)
    async def _on_field_error(_: Request, exc: schema.FieldError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.exception_handler(PersonaError)
    async def _on_persona_error(_: Request, exc: PersonaError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def _on_any(_: Request, exc: Exception) -> JSONResponse:
        logger.warning("后台接口出错：%s", exc, exc_info=True)
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    # ------------------------------------------------------------ 页面
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    async def healthz() -> Dict[str, Any]:
        return {"ok": True, "uptime": round(time.time() - state.started_at, 1)}

    # ------------------------------------------------------------ 状态
    @app.get("/api/status")
    async def api_status() -> Dict[str, Any]:
        snap = state.snapshot()
        snap["ok"] = True
        snap["memory"] = _safe(lambda: _memory_stats(bot), {})
        snap["kb"] = _safe(lambda: _kb_stats(bot), {})
        snap["mode"] = "run" if bot is not None else "web"
        snap["panel"] = f"{cfg.get('monitor.host', '127.0.0.1')}:{cfg.get('monitor.port', 8787)}"
        snap["window"] = _safe(
            lambda: (bot.bridge.window_state()
                     if (bot is not None and getattr(bot, "bridge", None) is not None) else {}),
            {},
        )
        # 降级状态必须能看见：向量模型挂了会静默退化成关键词检索，
        # 配置没落盘会让重启后参数回退——这两件事以前在面板上完全不可见。
        embed_ok: Optional[bool] = None
        if bot is not None:
            embed_ok = _safe(lambda: bool(bot.embedder.available), None)
        snap["health"] = {
            "embedder": embed_ok,
            "override_saved": not bool(getattr(cfg, "save_error", "")),
            "override_error": str(getattr(cfg, "save_error", "") or ""),
        }
        # 等级与群记录都是「静默降级」型：坏了不会报错，只是不再记。
        # 面板必须能看到它到底有没有在工作，否则又是一件「以为在记、其实没记」。
        snap["people"] = _safe(
            lambda: _people(cfg, bot).describe(), {}
        )
        snap["group_log"] = _safe(
            lambda: _group_log(cfg, bot).stats(), {}
        )
        return snap

    # ------------------------------------------------------------ 配置
    @app.get("/api/config")
    async def api_config() -> Dict[str, Any]:
        return {
            "ok": True,
            "groups": schema.groups(),
            "fields": schema.describe(cfg),
            "override_file": str(getattr(cfg, "override_path", "") or ""),
        }

    @app.post("/api/config")
    async def api_set(payload: Dict[str, Any]) -> Dict[str, Any]:
        path = str(payload.get("path") or "").strip()
        value = schema.coerce(path, payload.get("value"))
        with _WRITE_LOCK:
            cfg.set_override(path, value)
            _apply(bot)
        logger.info("面板改参数：%s = %r", path, value)
        failed = _save_failed(cfg, path)
        if failed is not None:
            return failed
        return {"ok": True, "path": path, "value": cfg.get(path)}

    @app.post("/api/config/reset")
    async def api_reset(payload: Dict[str, Any]) -> Dict[str, Any]:
        path = str(payload.get("path") or "").strip()
        if path not in schema.BY_PATH:
            raise schema.FieldError(f"没有这个参数：{path}")
        with _WRITE_LOCK:
            cfg.clear_override(path)
            _apply(bot)
        logger.info("面板恢复默认：%s", path)
        failed = _save_failed(cfg, path)
        if failed is not None:
            return failed
        return {"ok": True, "path": path, "value": cfg.get(path)}

    # ------------------------------------------------------------ 动作
    @app.post("/api/action/reindex")
    async def api_reindex() -> Dict[str, Any]:
        from ..kb_store import KbStore
        from ..tools.kb import build_index

        store = getattr(bot, "kb", None) if bot is not None else None
        if store is None:
            store = KbStore(cfg.data_dir / "kb.sqlite3")
        files, chunks = build_index(store, cfg)
        stats = store.stats()
        logger.info("面板重建索引：%d 篇更新，%d 块", files, chunks)
        return {"ok": True, "files": files, "chunks": chunks, **stats}

    @app.post("/api/action/snapshot")
    async def api_snapshot() -> Dict[str, Any]:
        if bot is None:
            return {"ok": False, "error": "机器人没在跑，没有记忆可落盘"}
        bot.store.snapshot()
        return {"ok": True, "memory": _memory_stats(bot)}

    @app.post("/api/action/clear-recent")
    async def api_clear_recent() -> Dict[str, Any]:
        state.clear_recent()
        return {"ok": True}

    @app.post("/api/action/reset-all")
    async def api_reset_all() -> Dict[str, Any]:
        with _WRITE_LOCK:
            cfg.clear_all_overrides()
            _apply(bot)
        logger.info("面板把全部参数恢复默认")
        failed = _save_failed(cfg, "")
        if failed is not None:
            return failed
        return {"ok": True}

    # ------------------------------------------------------------ 人设（多角色）
    @app.get("/api/personas")
    async def api_personas() -> Dict[str, Any]:
        store = _personas(cfg, bot)
        info = store.describe()
        # 「绑了几个会话」的唯一事实来源是 chats.json，这里现算，不另存
        bound: Dict[str, int] = {}
        for pref in _chats(cfg, bot).all().values():
            name = str(pref.get("persona") or "")
            if name:
                bound[name] = bound.get(name, 0) + 1
        for item in info["items"]:
            item["bound"] = bound.get(item["name"], 0)
        info["bindings"] = {k: v.get("persona") for k, v in _chats(cfg, bot).all().items() if v.get("persona")}
        return {"ok": True, **info}

    @app.get("/api/persona")
    async def api_persona(name: str = "") -> Dict[str, Any]:
        store = _personas(cfg, bot)
        target = (name or "").strip() or store.default_name
        if not store.exists(target):
            raise schema.FieldError(f"没有叫「{target}」的角色")
        text = store.text(target)
        return {
            "ok": True,
            "name": target,
            "is_default": target == store.default_name,
            "path": str(getattr(store, "dir", "")),
            "text": text,
            "chars": len(text),
            "lines": len([x for x in text.splitlines() if x.strip()]),
        }

    @app.post("/api/persona")
    async def api_persona_save(payload: Dict[str, Any]) -> Dict[str, Any]:
        store = _personas(cfg, bot)
        name = str(payload.get("name") or "").strip() or store.default_name
        store.save(name, str(payload.get("text") or ""))
        if bot is not None:
            bot.reload_persona()
        logger.info("面板保存角色：%s", name)
        return {"ok": True, "name": name, "chars": len(store.text(name))}

    @app.post("/api/personas/new")
    async def api_persona_new(payload: Dict[str, Any]) -> Dict[str, Any]:
        store = _personas(cfg, bot)
        name = store.create(str(payload.get("name") or ""), str(payload.get("text") or ""))
        return {"ok": True, "name": name}

    @app.post("/api/personas/rename")
    async def api_persona_rename(payload: Dict[str, Any]) -> Dict[str, Any]:
        store = _personas(cfg, bot)
        old = str(payload.get("from") or "")
        new = store.rename(old, str(payload.get("to") or ""))
        # 绑定存在 chats.json 里，角色库改不到那份文件，由这里跟着改
        chats = _chats(cfg, bot)
        moved = 0
        for chat_id, pref in chats.all().items():
            if str(pref.get("persona") or "") == old:
                chats.set(chat_id, persona=new, name=str(pref.get("name") or ""))
                moved += 1
        if bot is not None:
            bot.reload_persona()
        logger.info("面板角色改名：%s → %s（跟着改了 %d 个会话绑定）", old, new, moved)
        return {"ok": True, "name": new, "moved": moved}

    @app.post("/api/personas/delete")
    async def api_persona_delete(payload: Dict[str, Any]) -> Dict[str, Any]:
        store = _personas(cfg, bot)
        name = str(payload.get("name") or "")
        store.delete(name)
        # 被删角色的会话绑定要一起清掉，否则 chats.json 里留着指向空角色的绑定，
        # 面板显示「绑定了 xxx」但实际用的是默认角色，看着莫名其妙。
        chats = _chats(cfg, bot)
        cleared = 0
        for chat_id, pref in chats.all().items():
            if str(pref.get("persona") or "") == name:
                chats.set(chat_id, persona="", name=str(pref.get("name") or ""))
                cleared += 1
        if bot is not None:
            bot.reload_persona()
        logger.info("面板删除角色：%s（清掉 %d 个会话绑定）", name, cleared)
        return {"ok": True, "cleared": cleared}

    @app.post("/api/personas/default")
    async def api_persona_default(payload: Dict[str, Any]) -> Dict[str, Any]:
        store = _personas(cfg, bot)
        name = store.set_default(str(payload.get("name") or ""))
        if bot is not None:
            bot.reload_persona()
        return {"ok": True, "name": name}

    # ------------------------------------------------------------ 会话监视
    @app.get("/api/chats")
    async def api_chats() -> Dict[str, Any]:
        from ..chat_policies import MODE_HINT, MODE_LABEL, MODES, PEOPLE_LABEL, is_group

        store = _personas(cfg, bot)
        defaults = store.default_name
        prefs = _chats(cfg, bot).all()
        items: List[Dict[str, Any]] = []
        seen = set()

        if bot is not None and getattr(bot, "bridge", None) is not None:
            try:
                rows = bot.bridge.sessions(limit=60)
            except Exception as exc:  # noqa: BLE001
                logger.warning("面板读会话列表失败：%s", exc)
                rows = []
            for row in rows or []:
                chat_id = str(row.get("username") or "")
                if not chat_id:
                    continue
                seen.add(chat_id)
                pref = prefs.get(chat_id) or {}
                items.append(
                    {
                        "id": chat_id,
                        "name": bot.bridge.display_name(chat_id) or chat_id,
                        "group": is_group(chat_id),
                        "summary": str(row.get("summary") or "")[:60],
                        "last_time": int(row.get("last_time") or 0),
                        "unread": int(row.get("unread") or 0),
                        "mode": pref.get("mode") or "",
                        "persona": pref.get("persona") or "",
                        "people_mode": pref.get("people_mode") or "",
                        "people": list(pref.get("people") or []),
                        "explicit": bool(pref),
                    }
                )

        # 设过策略、但最近没聊过的会话也列出来，否则改了策略就找不着了
        for chat_id, pref in prefs.items():
            if chat_id in seen:
                continue
            items.append(
                {
                    "id": chat_id,
                    "name": pref.get("name") or chat_id,
                    "group": is_group(chat_id),
                    "summary": "",
                    "last_time": int(pref.get("updated") or 0),
                    "unread": 0,
                    "mode": pref.get("mode") or "",
                    "persona": pref.get("persona") or "",
                    "people_mode": pref.get("people_mode") or "",
                    "people": list(pref.get("people") or []),
                    "explicit": True,
                }
            )

        return {
            "ok": True,
            "defaults": {
                "group": "at" if cfg.get("wechat.group_at_only", True) else "auto",
                "private": "auto" if cfg.get("wechat.reply_private", True) else "off",
                "persona": defaults,
            },
            "modes": [{"key": m, "label": MODE_LABEL[m], "hint": MODE_HINT[m]} for m in MODES],
            "people_modes": [
                {"key": k, "label": PEOPLE_LABEL[k]} for k in ("", "only", "ignore")
            ],
            "personas": store.names(),
            "items": items,
        }

    @app.get("/api/chats/people")
    async def api_chat_people(id: str = "", limit: int = 200) -> Dict[str, Any]:
        """列这个会话里最近说过话的人，给面板的「只理/不理」名单用。"""
        from ..chat_policies import is_group

        chat_id = str(id or "").strip()
        if not chat_id:
            raise schema.FieldError("没有会话 id")
        pref = _chats(cfg, bot).get(chat_id)
        rule = {
            "people_mode": str(pref.get("people_mode") or ""),
            "people": list(pref.get("people") or []),
        }
        if bot is None or getattr(bot, "bridge", None) is None:
            return {"ok": True, "group": is_group(chat_id), "items": [], **rule}

        rows = []
        try:
            rows = bot.bridge.recent_messages(chat_id, limit=max(20, min(limit, 500))) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("读会话成员失败：%s", exc)

        counts: Dict[str, Dict[str, Any]] = {}
        self_wxid = ""
        try:
            self_wxid = bot.bridge.self_wxid or ""
        except Exception:  # noqa: BLE001
            pass
        for row in rows:
            sender = str(row.get("sender_username") or "")
            if not sender or sender == self_wxid:
                continue
            item = counts.setdefault(
                sender, {"id": sender, "name": "", "count": 0, "last": 0}
            )
            item["count"] += 1
            item["last"] = max(item["last"], int(row.get("create_time") or 0))
        for sender, item in counts.items():
            try:
                item["name"] = bot.bridge.display_name(sender) or sender
            except Exception:  # noqa: BLE001
                item["name"] = sender
        items = sorted(counts.values(), key=lambda x: (-x["count"], -x["last"]))
        return {"ok": True, "group": is_group(chat_id), "items": items[:60], **rule}

    @app.post("/api/chats")
    async def api_chat_set(payload: Dict[str, Any]) -> Dict[str, Any]:
        chat_id = str(payload.get("id") or "").strip()
        mode = payload.get("mode")
        persona = payload.get("persona")
        name = str(payload.get("name") or "")
        people_mode = payload.get("people_mode")
        people = payload.get("people")
        if persona:
            # 绑角色前先确认这个角色真的存在，别写进去一个空指针
            if not _personas(cfg, bot).exists(str(persona)):
                raise schema.FieldError(f"没有叫「{persona}」的角色")
        if people is not None and not isinstance(people, (list, tuple)):
            raise schema.FieldError("名单要是数组")
        try:
            pref = _chats(cfg, bot).set(
                chat_id,
                mode=None if mode is None else str(mode),
                persona=None if persona is None else str(persona),
                name=name,
                people_mode=None if people_mode is None else str(people_mode),
                people=None if people is None else [str(x) for x in people],
            )
        except ValueError as exc:
            raise schema.FieldError(str(exc)) from None
        logger.info("面板改会话策略：%s → %s / %s", chat_id, pref.get("mode") or "默认",
                    pref.get("persona") or "默认角色")
        return {"ok": True, "chat": pref}

    @app.post("/api/chats/forget")
    async def api_chat_forget(payload: Dict[str, Any]) -> Dict[str, Any]:
        _chats(cfg, bot).forget(str(payload.get("id") or ""))
        return {"ok": True}

    # ------------------------------------------------------------ 记忆
    @app.get("/api/memory")
    async def api_memory(limit: int = 60, q: str = "", owner: str = "",
                         scope: str = "") -> Dict[str, Any]:
        if bot is None:
            return {"ok": True, "items": [], "hits": [], "stats": {}, "query": q,
                    "owners": [], "legacy": 0}
        # owner 的三态是刻意的，别合并成一个「过滤条件」：
        #   空串        = 不过滤（老面板、自检脚本都还是这么调的，行为不能变）
        #   __legacy__  = 只看未归属那批旧数据（面板最需要把它们捞出来补救）
        #   其它值      = 精确匹配 u:xxx / g:xxx
        # 如果把空串理解成「只看未归属」，打开本区默认就只剩那 19 条旧数据，
        # 新记忆一条都看不见——用户会以为记忆库被清空了。
        owner_filter: Optional[str] = None
        if owner == LEGACY_OWNER:
            owner_filter = ""
        elif owner.strip():
            owner_filter = owner.strip()
        scope_filter: Optional[str] = scope.strip() or None

        items = sorted(
            (m for m in bot.store.active(owner=owner_filter, scope=scope_filter)),
            key=lambda m: m.created_at,
            reverse=True,
        )[: max(1, min(limit, 300))]
        people = _safe(lambda: _people(cfg, bot), None)
        hits: List[Dict[str, Any]] = []
        if q.strip():
            # 检索也跟着筛选走：不然切到「未归属」之后，下面的命中列表还在
            # 吐别人的记忆，看起来就像筛选根本没生效。
            for hit in bot.retriever.search(
                    q, mark_used=False, owner=owner_filter, scope=scope_filter)[:10]:
                hits.append(
                    {
                        "id": hit.memory.id,
                        "text": hit.memory.text,
                        "score": round(hit.score, 3),
                        "similarity": round(hit.similarity, 3),
                    }
                )
        return {
            "ok": True,
            "stats": bot.store.stats(),
            "query": q,
            "hits": hits,
            "items": [_memory_row(m, people, bot) for m in items],
            "owners": _owner_options(bot, people),
            "legacy": bot.store.legacy_count(),
        }

    @app.post("/api/memory/assign")
    async def api_memory_assign(payload: Dict[str, Any]) -> Dict[str, Any]:
        if bot is None:
            return {"ok": False, "error": "机器人没在跑，改不了记忆库"}
        memory_id = str(payload.get("id") or "").strip()
        # 面板那个 prompt 只让人填 wxid（谁也记不住 u: 前缀），所以这里补前缀。
        # 但**只补没有冒号的**：已经写了冒号说明用户是有意指定类型，写错类型
        # 就交给 store 报 400，别偷偷补成 u:x:xxx —— 那种归属谁都不会去查。
        owner = str(payload.get("owner") or "").strip()
        if owner and ":" not in owner:
            owner = "u:" + owner
        scope = str(payload.get("scope") or "").strip()
        chat_id = str(payload.get("chat_id") or "").strip()
        try:
            mem = bot.store.assign(memory_id, owner, scope=scope, chat_id=chat_id)
        except ValueError as exc:
            raise schema.FieldError(str(exc)) from None
        if mem is None:
            raise schema.FieldError("没有这条记忆（可能已经被作废了）")
        # 和作废一样立刻落一次快照：指派是低频的人工操作，多写一次快照换
        # 「点了就一定记住了」很划算，不用等攒够 snapshot_every 次追加。
        bot.store.snapshot()
        people = _safe(lambda: _people(cfg, bot), None)
        owner_name = _owner_name(mem.owner, people, bot)
        logger.info("面板指派记忆 %s → %s（%s）", memory_id, mem.owner, mem.text[:24])
        return {"ok": True, "stats": bot.store.stats(), "id": mem.id,
                "owner": mem.owner, "owner_name": owner_name}

    @app.post("/api/memory/void")
    async def api_memory_void(payload: Dict[str, Any]) -> Dict[str, Any]:
        if bot is None:
            return {"ok": False, "error": "机器人没在跑，改不了记忆库"}
        memory_id = str(payload.get("id") or "").strip()
        mem = bot.store.get(memory_id)
        if mem is None:
            raise schema.FieldError("没有这条记忆")
        ok = bot.store.void(memory_id, reason="面板手工作废")
        bot.store.snapshot()
        logger.info("面板作废记忆 %s：%s", memory_id, mem.text[:24])
        return {"ok": bool(ok), "stats": bot.store.stats()}

    # ------------------------------------------------------------ 资料库
    @app.get("/api/kb")
    async def api_kb() -> Dict[str, Any]:
        store = _kb_store(cfg, bot)
        return {
            "ok": True,
            "stats": store.stats(),
            "docs": store.list_docs(limit=200),
            "dirs": cfg.get("kb.dirs", []),
        }

    # ------------------------------------------------------------ 工具
    @app.get("/api/tools")
    async def api_tools() -> Dict[str, Any]:
        from ..tools import TurnContext
        from ..tools import build as build_tools

        store = getattr(bot, "store", None)
        try:
            registry = build_tools(
                cfg,
                store,
                TurnContext(chat_id="panel", chat_name="panel"),
                reader=getattr(bot, "reader", None),
                kb=getattr(bot, "kb", None),
            )
            specs = registry.specs()
        except Exception as exc:  # noqa: BLE001
            logger.warning("面板列工具失败：%s", exc)
            specs = []
        snapshot = state.snapshot().get("tools") or {}
        items = []
        for spec in specs:
            fn = spec.get("function") or {}
            name = str(fn.get("name") or "")
            stat = snapshot.get(name) or {}
            items.append(
                {
                    "name": name,
                    "description": str(fn.get("description") or ""),
                    "params": list(((fn.get("parameters") or {}).get("properties") or {}).keys()),
                    "count": int(stat.get("count") or 0),
                    "fail": int(stat.get("fail") or 0),
                    "avg_ms": float(stat.get("avg_ms") or 0),
                }
            )
        return {"ok": True, "items": items}

    # ------------------------------------------------------------ 语音试听
    @app.post("/api/voice/test")
    async def api_voice_test(payload: Dict[str, Any]) -> Dict[str, Any]:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise schema.FieldError("先写一句要念的话")
        from ..voice.tts import TTS

        tts = getattr(bot, "tts", None) if bot is not None else None
        if tts is None:
            tts = TTS(
                out_dir=cfg.data_dir / "voice",
                voice=str(cfg.get("voice.voice", "zh-CN-XiaoxiaoNeural")),
                rate=str(cfg.get("voice.rate", "+8%")),
                pitch=str(cfg.get("voice.pitch", "+0Hz")),
                max_chars=int(cfg.get("voice.tts_max_chars", 220)),
            )
        path = tts.synth(text)
        if path is None:
            raise schema.FieldError("合成失败：看日志（多半是没联网或音色名写错了）")
        return {"ok": True, "file": path.name, "url": f"/api/voice/{path.name}"}

    @app.get("/api/voice/{name}")
    async def api_voice_file(name: str) -> Any:
        from fastapi.responses import FileResponse

        safe = Path(name).name  # 挡掉 ../ 之类的路径穿越
        path = cfg.data_dir / "voice" / safe
        if not path.exists() or path.suffix != ".mp3":
            return JSONResponse({"ok": False, "error": "没有这个音频"}, status_code=404)
        return FileResponse(str(path), media_type="audio/mpeg")

    # ------------------------------------------------------------ 关键用户 / 群记录
    @app.get("/api/people")
    async def api_people() -> Dict[str, Any]:
        """谁被设成了什么等级。种子（config 里填的）也一并回，但标出来源。"""
        store = _people(cfg, bot)
        return {"ok": True, **store.describe(), "items": store.all()}

    @app.post("/api/people")
    async def api_people_set(payload: Dict[str, Any]) -> Dict[str, Any]:
        store = _people(cfg, bot)
        try:
            item = store.set(
                str(payload.get("wxid") or ""),
                level=payload.get("level"),
                alias=payload.get("alias"),
                note=payload.get("note"),
            )
        except ValueError as exc:
            # 等级写错是用户操作问题，属于 400，不是 500
            raise schema.FieldError(str(exc)) from None
        logger.info("面板改等级：%s -> %s", payload.get("wxid"), item.get("level"))
        return {"ok": True, "item": item}

    @app.post("/api/people/forget")
    async def api_people_forget(payload: Dict[str, Any]) -> Dict[str, Any]:
        """删掉某个 wxid 的等级记录，回落到种子/默认等级。

        找不到也返回成功：面板只关心「现在没了」，不关心「刚才到底有没有」。
        """
        store = _people(cfg, bot)
        wxid = str(payload.get("wxid") or "").strip()
        ok = store.forget(wxid)
        logger.info("面板删除等级记录：%s（%s）", wxid, "有" if ok else "本来就没有")
        return {"ok": True, "removed": bool(ok)}

    @app.get("/api/group-log")
    async def api_group_log(chat: str = "", sender: str = "",
                            limit: int = 100) -> Dict[str, Any]:
        """关键用户在群里说过的话。只读流水，不参与检索、不改判定。

        两种行混在一起（`op` 区分）：`msg` 是说的一句，`reply` 是机器人的回复。
        按时间正序返回，前端按 `op` 分开渲染。
        """
        store = _group_log(cfg, bot)
        items = store.read(chat_id=chat, sender=sender, limit=limit)
        return {"ok": True, "items": items, "stats": store.stats()}

    # ------------------------------------------------------------ 系统信息
    @app.get("/api/system")
    async def api_system() -> Dict[str, Any]:
        import platform
        import sys

        from .. import settings as settings_mod

        overrides = dict(getattr(cfg, "overrides", {}) or {})
        deps = {
            dist: _version_of(dist)
            for dist in (
                "openai", "fastapi", "uvicorn", "pyyaml", "requests", "numpy",
                "fastembed", "edge-tts", "pypdf", "wechatauto-replica",
            )
        }
        return {
            "ok": True,
            "root": str(settings_mod.ROOT),
            "data_dir": str(cfg.data_dir),
            "config": str(cfg.rel("config.yaml")),
            "override": str(getattr(cfg, "override_path", "") or ""),
            "persona": str(_persona_path(cfg)),
            "log": str(cfg.rel(cfg.get("log.file", "data/bot.log"))),
            "kb": str(cfg.data_dir / "kb.sqlite3"),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pid": os.getpid(),
            "panel": f"{cfg.get('monitor.host', '127.0.0.1')}:{cfg.get('monitor.port', 8787)}",
            "mode": "run" if bot is not None else "web",
            "overrides": overrides,
            "deps": deps,
        }

    return app


# ------------------------------------------------------------------ 内部
def _save_failed(cfg: Any, path: str) -> Optional[JSONResponse]:
    """配置只改了内存、没落盘时，别对面板说 ok。

    以前这里只 print 一行，接口照样返回成功——用户以为参数改好了，
    重启才发现回到旧值，是典型的「降级被当成成功」。
    """
    reason = str(getattr(cfg, "save_error", "") or "")
    if not reason:
        return None
    logger.error("参数落盘失败（%s）：%s", path or "全部", reason)
    return JSONResponse(
        {
            "ok": False,
            "path": path,
            "error": "内存里改了，但没写进 override.yaml（重启后会丢）：" + reason,
        },
        status_code=500,
    )


def _personas(cfg: Any, bot: Any) -> Any:
    """优先用机器人那份（同一状态文件），面板独立跑时自己建一个。"""
    store = getattr(bot, "personas", None) if bot is not None else None
    if store is not None:
        return store
    from ..personas import PersonaStore

    return PersonaStore(
        cfg.rel(cfg.get("persona.dir", "prompts/personas")),
        legacy_file=cfg.rel(cfg.get("persona.prompt_file", "prompts/persona.md")),
        state_path=cfg.data_dir / "personas.json",
    )


def _chats(cfg: Any, bot: Any) -> Any:
    store = getattr(bot, "chats", None) if bot is not None else None
    if store is not None:
        return store
    from ..chat_policies import ChatPrefs

    return ChatPrefs(cfg.data_dir / "chats.json")


def _people(cfg: Any, bot: Any) -> Any:
    """优先用机器人那份（同一状态文件），面板独立跑时自己建一个。"""
    store = getattr(bot, "people", None) if bot is not None else None
    if store is not None:
        return store
    from ..user_levels import UserLevelStore

    return UserLevelStore(
        cfg.data_dir / "people.json",
        owners=list(cfg.get("people.owners") or []),
        key_users=list(cfg.get("group.key_users") or []),
        default=str(cfg.get("people.default_level", "stranger")),
    )


def _group_log(cfg: Any, bot: Any) -> Any:
    store = getattr(bot, "glog", None) if bot is not None else None
    if store is not None:
        return store
    from ..group_log import GroupLog

    return GroupLog(
        cfg.data_dir / "group_log",
        days=int(cfg.get("group.log_days", 30)),
        enabled=bool(cfg.get("group.log_enabled", True)),
    )


def _persona_path(cfg: Any) -> Path:
    rel = cfg.get("persona.prompt_file", "prompts/persona.md")
    return cfg.rel(rel) if hasattr(cfg, "rel") else Path(rel)


def _kb_store(cfg: Any, bot: Any) -> Any:
    """优先用机器人那份（同一个连接，索引状态一致），没有就自己开一个只读的。"""
    store = getattr(bot, "kb", None) if bot is not None else None
    if store is not None:
        return store
    from ..kb_store import KbStore

    return KbStore(cfg.data_dir / "kb.sqlite3")


def _memory_row(mem: Any, people: Any = None, bot: Any = None) -> Dict[str, Any]:
    return {
        "id": mem.id,
        "text": mem.text,
        "alias": mem.alias_text or "",
        "type": mem.type,
        "importance": mem.importance,
        "use_count": mem.use_count,
        "status": mem.status,
        "note": mem.note or "",
        "created": time.strftime("%m-%d %H:%M", time.localtime(mem.created_at or 0)),
        "last_used": time.strftime("%m-%d %H:%M", time.localtime(mem.last_used_at))
        if mem.last_used_at
        else "—",
        # 归属三件套原样带出去：面板要拿 owner 做筛选、拿 owner_name 显示、
        # 拿 scope/chat_id 让人一眼看出这条是私聊还是某个群的公共记忆。
        "owner": mem.owner,
        "owner_name": _owner_name(mem.owner, people, bot),
        "scope": mem.scope,
        "chat_id": mem.chat_id,
    }


def _owner_name(owner: str, people: Any = None, bot: Any = None) -> str:
    """把归属串翻译成面板上给人看的名字。

    为什么不在记忆库里存名字：名字会变（备注改了、群名改了、人退群了），
    存下来就是第二份状态，迟早和 `data/people.json`、微信里的真名对不上，
    而且改一处另九处不知道——项目已经因为同一种错吃过一次苦。所以只在展示
    这一层翻译，库里永远只存 id。

    解析链（都失败就退到 id 本身，绝不返回空串——空串在面板上等于「没有归属」）：
      - `u:<wxid>` → people.json 的备注 alias → bridge 的显示名 → wxid 尾 6 位
      - `g:<chat_id>` → 「群 <前 8 位>」（群名要查微信，面板不开桥也渲染得出来）
      - `""` → 「未归属」
    """
    owner = str(owner or "").strip()
    if not owner:
        return "未归属"
    prefix, _, key = owner.partition(":")
    if prefix == "g":
        return f"群 {key[:8] or key}"
    if prefix == "u":
        alias = ""
        if people is not None:
            try:
                alias = str(people.alias_of(key) or "").strip()
            except Exception:  # noqa: BLE001
                alias = ""
        if alias:
            return alias
        # bridge 可能是 None（面板单独跑、自检脚本里都没有微信），必须容错：
        # 这里挂掉就等于整张记忆表都渲染不出来。
        bridge = getattr(bot, "bridge", None) if bot is not None else None
        if bridge is not None:
            try:
                name = str(bridge.display_name(key) or "").strip()
            except Exception:  # noqa: BLE001
                name = ""
            # display_name 查不到昵称时会把 id 原样返回，那种情况别当成名字用
            if name and name != key:
                return name
        return key[-6:] if len(key) > 6 else (key or owner)
    # 认不出来的写法（比如早期手填的裸 wxid）：原样显示，好过显示「未归属」骗人
    return owner


def _owner_options(bot: Any, people: Any = None) -> List[Dict[str, Any]]:
    """面板「归属」下拉的选项：按条数降序，未归属单独排最后。

    未归属固定 0 条时不出这个选项：下拉里放一个「未归属（0）」点进去是空表，
    看着像坏了。它消失 = 那批旧数据已经全指派完了，正好是个完成信号。
    """
    counts = _safe(lambda: bot.store.owner_counts(), {}) if bot is not None else {}
    items = [
        {"owner": key, "name": _owner_name(key, people, bot), "count": int(count)}
        for key, count in counts.items() if key
    ]
    # 条数一样时按名字排，避免每次刷新下拉顺序抖一下（id 顺序是 dict 插入序，不稳定）
    items.sort(key=lambda x: (-x["count"], x["name"]))
    legacy = int(counts.get("", 0) or 0)
    if legacy:
        items.append({"owner": LEGACY_OWNER, "name": "未归属", "count": legacy})
    return items


def _version_of(dist: str) -> str:
    """只查包元数据，**不 import**。

    为什么不用 import 探测：wechatauto 一导入就会把 root 上所有 handler 清掉
    （见 app/wechat/bridge.py 的说明），面板的日志环会当场失效——点一下「系统」
    页日志就没了，这坑踩过一次。
    """
    from importlib import metadata

    for name in (dist, dist.replace("-", "_"), dist.replace("_", "-")):
        try:
            return metadata.version(name)
        except Exception:  # noqa: BLE001
            continue
    return "未安装"


def _apply(bot: Any) -> None:
    """把改动推到运行时对象上。bot 不在（web 模式）就只改内存配置。"""
    if bot is None:
        return
    try:
        bot.apply_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("参数改了但热应用失败（重启后生效）：%s", exc)


def _safe(fn: Any, default: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _memory_stats(bot: Any) -> Dict[str, Any]:
    if bot is None:
        return {}
    return bot.store.stats()


def _kb_stats(bot: Any) -> Dict[str, Any]:
    stats = getattr(bot, "kb", None)
    if stats is None:
        return {}
    return stats.stats()


def start_in_thread(app: FastAPI, host: str = "127.0.0.1",
                    port: int = 8787) -> Optional[Any]:
    """在后台线程里起 uvicorn。

    Returns:
        uvicorn.Server，起不来（比如端口被占）返回 None。
    """
    import uvicorn

    # log_config=None 很关键：uvicorn 默认会 dictConfig 一把，把 root 上的
    # 控制台/文件 handler 全换掉，于是机器人日志从这一秒开始消失。
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="monitor", daemon=True)
    thread.start()

    for _ in range(60):
        if getattr(server, "started", False):
            return server
        if not thread.is_alive():
            break
        time.sleep(0.1)

    logger.error("后台面板没起来（%s:%s 可能被占用），机器人照常跑", host, port)
    try:
        server.should_exit = True
    except Exception:  # noqa: BLE001
        pass
    return None


def stop(server: Any) -> None:
    if server is None:
        return
    try:
        server.should_exit = True
    except Exception:  # noqa: BLE001
        pass
