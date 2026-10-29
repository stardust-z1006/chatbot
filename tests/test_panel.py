# -*- coding: utf-8 -*-
"""回归：后台面板（真起一个 HTTP 服务，不接微信、不调模型）。

验的是四件事：
  1. 页面和接口能起来，状态里能读出计数
  2. 改参数：类型校验、越界拒绝、落盘 override.yaml、机器人侧立刻拿到新值
  3. 恢复默认：值回到 config.yaml 里的原值，override 文件用完自己消失
  4. 重建索引动作能跑通（空库也能安全返回）

跑法：
    .venv\\Scripts\\python.exe tests\\test_panel.py
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from app.bot import Bot
from app.panel import server as monitor
from app.memory import MemoryStore
from app.panel.state import (
    MonitorState,
    attach_log_handler,
    detach_log_handler,
)
from app.settings import load
from _support import FAILS, check

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def get(path: str) -> Dict[str, Any]:
    res = requests.get(BASE + path, timeout=5)
    return res.json()


def post(path: str, payload: Dict[str, Any]) -> Any:
    res = requests.post(BASE + path, json=payload, timeout=15)
    try:
        return res.status_code, res.json()
    except ValueError:
        return res.status_code, {"raw": res.text[:200]}


def _field(payload: Dict[str, Any], path: str) -> Dict[str, Any]:
    for item in payload.get("fields") or []:
        if item.get("path") == path:
            return item
    return {}


def main() -> int:
    print("=" * 52)
    print(" 后台面板回归（127.0.0.1:%d）" % PORT)
    print("=" * 52)

    # 全程跑在临时目录里。这份脚本以前直接读写真实的 data/、prompts/：
    # 一次回归就会改掉用户的面板配置、人设、会话策略、记忆库和资料索引，
    # 中途失败还不回滚。现在只复制 config.yaml 和 prompts/，data/knowledge 新建，
    # 跑完整个目录删掉。
    # 副产物：临时目录里没有 .env，所以裁判拿不到 key，天然不会联网调模型。
    work = Path(tempfile.mkdtemp(prefix="deepchat-test-监控-"))
    shutil.copy2(ROOT / "config.yaml", work / "config.yaml")
    shutil.copytree(ROOT / "prompts", work / "prompts")
    (work / "data").mkdir()
    (work / "knowledge").mkdir()
    print(f" 临时根目录：{work}")

    cfg = load(work)
    state = MonitorState()
    handler = attach_log_handler(state)
    bot = Bot(cfg, state=state)
    app = monitor.create_app(cfg, bot, state)
    server = monitor.start_in_thread(app, "127.0.0.1", PORT)
    if server is None:
        print("端口被占，换一个再试")
        return 1

    try:
        # ---------------------------------------------------------- 1. 起来了吗
        print("\n1. 页面与接口")
        html = requests.get(BASE + "/", timeout=5).text
        check("首页是 HTML", "<title>deepChat · 后台</title>" in html, f"{len(html)} 字节")
        st = get("/api/status")
        check("status 带运行标记", st.get("ok") is True and "running" in st)
        check("web 进程里 running=False", st["running"] is False)
        check("内存/资料库状态带出来了", isinstance(st.get("memory"), dict) and isinstance(st.get("kb"), dict))

        cfg_api = get("/api/config")
        # 不写死条数：加一个参数就要改一次断言的测试，等于每次都在误报
        check("配置清单至少 48 项", len(cfg_api["fields"]) >= 48, str(len(cfg_api["fields"])))
        paths = [f["path"] for f in cfg_api["fields"]]
        check("参数路径不重复", len(set(paths)) == len(paths))
        check("分组顺序固定", cfg_api["groups"][0] == "chat", cfg_api["groups"][0])
        check("每个参数都带 label/hint", all(f.get("label") and "hint" in f for f in cfg_api["fields"]))

        # 灌一点假数据，状态页才有东西看
        state.tool("now", 12.0, True)
        state.tool("web_search", 3200.0, False)
        state.message("filehelper", "文件传输助手", "现在几点", "一点半", 1.8, ["now"], True)
        st = get("/api/status")
        check("计数进去", st["messages"] == 1 and st["recent"][0]["text"] == "现在几点")
        check("工具统计进去了", st["tools"]["web_search"]["fail"] == 1)

        # ---------------------------------------------------------- 2. 改参数
        print("\n2. 改参数（wechat.bubble_chars 40 → 60）")
        code, data = post("/api/config", {"path": "wechat.bubble_chars", "value": 60})
        check("改成功", code == 200 and data["value"] == 60, str(data))
        check("内存里立刻是新值", cfg.get("wechat.bubble_chars") == 60)
        check("机器人拿到的也是新值（同一个 cfg）", bot.cfg.get("wechat.bubble_chars") == 60)
        override = cfg.override_path.read_text(encoding="utf-8")
        check("落盘到 override.yaml", "bubble_chars: 60" in override, str(cfg.override_path.name))
        field = _field(get("/api/config"), "wechat.bubble_chars")
        check("面板标成「已改」", field.get("overridden") is True and field.get("value") == 60)
        check("默认值仍显示 40", field.get("default") == 40)

        print("\n3. 非法值要挡住")
        code, data = post("/api/config", {"path": "wechat.bubble_chars", "value": 5})
        check("小于下限被拒", code == 400 and not data.get("ok"), str(data.get("error")))
        code, data = post("/api/config", {"path": "wechat.bubble_chars", "value": "abc"})
        check("不是数字被拒", code == 400, str(data.get("error")))
        code, data = post("/api/config", {"path": "不存在的路径", "value": 1})
        check("未知参数被拒", code == 400, str(data.get("error")))
        code, data = post("/api/config", {"path": "voice.mode", "value": "有时候"})
        check("枚举外值被拒", code == 400, str(data.get("error")))
        check("拒掉之后值没被改", cfg.get("wechat.bubble_chars") == 60)

        print("\n4. 布尔和列表")
        code, data = post("/api/config", {"path": "voice.enabled", "value": True})
        check("开语音", code == 200 and cfg.get("voice.enabled") is True, str(data.get("value")))
        check("tts 跟着建起来了（热应用生效）", bot.tts is not None)
        code, data = post("/api/config", {"path": "kb.dirs", "value": "knowledge，手册"})
        check("列表按逗号拆开（中文逗号也行）", code == 200 and data["value"] == ["knowledge", "手册"], str(data.get("value")))

        # ---------------------------------------------------------- 5. 恢复默认
        print("\n5. 恢复默认")
        code, data = post("/api/config/reset", {"path": "wechat.bubble_chars"})
        check("回到 40", code == 200 and data["value"] == 40, str(data.get("value")))
        code, data = post("/api/config/reset", {"path": "voice.enabled"})
        check("语音关回去", data["value"] is False, str(data.get("value")))
        code, data = post("/api/config/reset", {"path": "kb.dirs"})
        check("目录回到 knowledge", data["value"] == ["knowledge"], str(data.get("value")))
        check("override 文件用完自己删掉", not cfg.override_path.exists(), str(cfg.override_path))

        # ---------------------------------------------------------- 6. 动作
        print("\n6. 动作接口")
        code, data = post("/api/action/reindex", {})
        check("重建索引能跑（空库）", code == 200 and data.get("ok") and "docs" in data, str(data))

        # 死索引要清掉：出现过「源文件删了、索引还在，机器人照旧文档念」的情况。
        # 判据是「文件不存在」，所以这里建一篇、索引、删掉、再索引，看它有没有留下。
        (work / "knowledge" / "会消失的资料.md").write_text(
            "# 会消失的资料\n\n这段话在资料删掉之后不该还能查到。\n", encoding="utf-8")
        post("/api/action/reindex", {})
        before = get("/api/kb")["stats"]["docs"]
        (work / "knowledge" / "会消失的资料.md").unlink()
        post("/api/action/reindex", {})
        after = get("/api/kb")["stats"]["docs"]
        check("索引跟着源文件走（删了就不再念旧文档）", before == 1 and after == 0,
              f"删前 {before} 篇 / 删后 {after} 篇")

        code, data = post("/api/action/snapshot", {})
        check("记忆快照能落盘", code == 200 and data.get("ok"), str(data))
        code, data = post("/api/action/clear-recent", {})
        check("清空面板记录", code == 200 and get("/api/status")["recent"] == [])
        code, data = post("/api/action/reset-all", {})
        check("一键全恢复默认", code == 200 and data.get("ok"))

        # ---------------------------------------------------------- 7. 角色库与会话监视
        print("\n7. 角色库")
        persona = get("/api/persona")
        check("读到默认角色", persona["ok"] and persona["chars"] > 20,
              f"{persona['name']} / {persona['chars']} 字")
        roles = get("/api/personas")
        check("角色清单能列", roles["ok"] and roles["default"] in [x["name"] for x in roles["items"]],
              str([x["name"] for x in roles["items"]]))

        code, data = post("/api/personas/new", {"name": "演示角色"})
        check("新建角色", code == 200 and data["name"] == "演示角色", str(data))
        code, data = post("/api/personas/new", {"name": "演示角色"})
        check("重名被拒", code == 400, str(data.get("error")))
        code, data = post("/api/personas/new", {"name": "a/b"})
        check("名字里的斜杠被洗掉", code == 200 and data["name"] == "ab", str(data.get("name")))
        post("/api/personas/delete", {"name": "ab"})

        text = "你是演示角色。\n说话只用五个字。"
        code, data = post("/api/persona", {"name": "演示角色", "text": text})
        check("保存角色内容", code == 200 and data["chars"] == len(text), str(data))
        check("读回一致", get("/api/persona?name=演示角色")["text"] == text.strip())
        code, data = post("/api/persona", {"name": "演示角色", "text": "   "})
        check("空内容被拒", code == 400, str(data.get("error")))

        code, data = post("/api/personas/default", {"name": "演示角色"})
        check("设为默认", code == 200 and get("/api/personas")["default"] == "演示角色")
        check("默认角色变了（bot 侧立刻生效）", bot.personas.default_name == "演示角色")
        post("/api/personas/default", {"name": persona["name"]})

        print("\n8. 会话监视")
        chats = get("/api/chats")
        check("会话接口能读", chats["ok"] and "modes" in chats and "defaults" in chats)
        check("模式是四种", [m["key"] for m in chats["modes"]] == ["auto", "at", "silent", "off"],
              str([m["key"] for m in chats["modes"]]))

        test_chat = "9999@chatroom"
        code, data = post("/api/chats", {"id": test_chat, "mode": "silent", "name": "演示群"})
        check("给群设「只看不回」", code == 200 and data["chat"]["mode"] == "silent", str(data))
        check("bot 侧立刻按新策略判", bot.chats.mode_for(test_chat, bot.cfg) == "silent")
        code, data = post("/api/chats", {"id": test_chat, "persona": "演示角色"})
        check("给群绑角色", code == 200 and data["chat"]["persona"] == "演示角色", str(data))
        check("这个群拿到的是演示角色人设",
              bot.persona_for(test_chat).startswith("你是演示角色"))
        hits = [x for x in get("/api/chats")["items"] if x["id"] == test_chat]
        check("设过策略的会话会列出来", len(hits) == 1 and hits[0]["explicit"])
        code, data = post("/api/chats", {"id": test_chat, "mode": "乱写"})
        check("非法模式被拒", code == 400, str(data.get("error")))

        print("\n9. 群里只理/不理某些人")
        code, data = post("/api/chats", {"id": test_chat, "people_mode": "only",
                                        "people": ["张三", "wxid_li"]})
        check("设成「只理这些人」", code == 200 and data["chat"]["people_mode"] == "only", str(data))
        check("名单里的人放行（按昵称）", bot.chats.allow_sender(test_chat, "wxid_zhang", "张三"))
        check("名单里的人放行（按 wxid）", bot.chats.allow_sender(test_chat, "wxid_li", "李四"))
        check("名单外的拦掉", bot.chats.allow_sender(test_chat, "wxid_wang", "王五") is False)
        code, data = post("/api/chats", {"id": test_chat, "people_mode": "ignore"})
        check("改成「不理这些人」", data["chat"]["people_mode"] == "ignore", str(data))
        check("名单外的放行", bot.chats.allow_sender(test_chat, "wxid_wang", "王五"))
        check("名单里的拦掉", bot.chats.allow_sender(test_chat, "wxid_zhang", "张三") is False)
        code, data = post("/api/chats", {"id": test_chat, "people_mode": "only", "people": []})
        check("空名单 + only 不会把群关死", bot.chats.allow_sender(test_chat, "wxid_any", "谁"))
        code, data = post("/api/chats", {"id": test_chat, "people_mode": "乱写"})
        check("非法成员规则被拒", code == 400, str(data.get("error")))
        rule = get("/api/chats/people?id=" + test_chat)
        check("成员接口能读规则", rule["ok"] and "people_mode" in rule, str(rule.get("people")))
        code, data = post("/api/chats", {"id": test_chat, "people_mode": "", "people": []})
        check("清空成员规则", data["chat"].get("people_mode") in (None, ""), str(data))

        code, data = post("/api/personas/rename", {"from": "演示角色", "to": "演示角色2"})
        check("角色改名", code == 200 and data["name"] == "演示角色2", str(data))
        check("改名会跟着改会话绑定",
              data.get("moved") == 1 and
              next(x for x in get("/api/chats")["items"] if x["id"] == test_chat)["persona"]
              == "演示角色2")
        post("/api/personas/rename", {"from": "演示角色2", "to": "演示角色"})

        print("\n10. 用户等级与关键用户记录")
        ppl = get("/api/people")
        check("等级接口列出四级",
              [x["key"] for x in ppl["levels"]] == ["owner", "friend", "stranger", "blocked"],
              str(len(ppl.get("levels") or [])))
        code, data = post("/api/people", {"wxid": "wxid_boss", "level": "owner",
                                          "alias": "老板"})
        check("设成主人", code == 200 and data["item"]["level"] == "owner", str(data))
        check("设完立刻生效（和机器人同一个 store）",
              bot.people.level_of("wxid_boss") == "owner" and bot.people.is_key("wxid_boss"))
        code, data = post("/api/people", {"wxid": "wxid_bad", "level": "king"})
        check("非法等级被拒", code == 400, str(data.get("error")))
        check("被拒之后没写进去", bot.people.level_of("wxid_bad") == "stranger")

        log = get("/api/group-log?limit=20")
        check("群记录接口能读", log["ok"] is True and isinstance(log["items"], list),
              str(log.get("stats")))
        st2 = get("/api/status")
        check("status 带群记录状态",
              isinstance(st2.get("group_log"), dict) and "today" in st2["group_log"],
              str(st2.get("group_log")))
        check("status 带等级概览",
              isinstance(st2.get("people"), dict) and "count" in st2["people"])

        # 接话打分的面板参数归到 people 组，删等级记录的 forget 接口也在这补验
        check("接话开关进了 people 组",
              _field(get("/api/config"), "group.engage.enabled").get("group") == "people")
        check("机器人别名进了 people 组",
              _field(get("/api/config"), "people.self_names").get("group") == "people")

        code, data = post("/api/people", {"wxid": "wxid_demo", "level": "friend", "alias": "老李"})
        check("设成熟人并带称呼", code == 200 and data["item"]["level"] == "friend", str(data))
        check("称呼跟着存下来",
              get("/api/people")["items"].get("wxid_demo", {}).get("alias") == "老李",
              str(get("/api/people")["items"].get("wxid_demo")))
        code, data = post("/api/people/forget", {"wxid": "wxid_demo"})
        check("forget 返回 removed", code == 200 and data.get("removed") is True, str(data))
        check("删完 items 里没有 wxid_demo", "wxid_demo" not in get("/api/people")["items"])
        code, data = post("/api/people", {"wxid": "wxid_demo", "level": "king"})
        check("非法等级仍 400（没被 forget 破坏）", code == 400, str(data.get("error")))

        print("\n11. 其余内容分区")
        bot.store.put("面板测试：对方在做一个微信机器人")
        mem = get("/api/memory?limit=10")
        check("记忆能列出来", mem["ok"] and len(mem["items"]) >= 1, f"{len(mem['items'])} 条")
        check("记忆行字段齐", all(k in mem["items"][0] for k in ("id", "text", "type", "importance")))
        mem_id = mem["items"][0]["id"]
        search = get("/api/memory?q=" + "微信机器人")
        check("语义检索能跑", search["ok"] and isinstance(search["hits"], list),
              f"{len(search['hits'])} 命中")
        code, data = post("/api/memory/void", {"id": mem_id})
        check("能作废一条", code == 200 and data["ok"], str(data.get("stats")))
        code, data = post("/api/memory/void", {"id": "不存在"})
        check("作废不存在的返回 400", code == 400, str(data.get("error")))

        # ---------------------------------------------------------- 12. 归属与指派
        print("\n12. 记忆归属：面板能看出「这条是谁的」，旧数据能指派补救")
        # 这一段验的是「失忆救不救得回来」：迁移前那批 owner="" 的记忆谁都检索不到，
        # 面板既看不出归属、也没法把它指定给某个人 —— 等于永久失忆。
        # 归属字段、归属筛选、指派动作，缺任何一个这批数据都只能等死。
        who = "u:wxid_demo_owner"
        made = bot.store.put("我的生日是三月十七号", owner="", scope="private")
        legacy_id = made["memory"].id
        legacy_before = get("/api/memory?limit=10")["legacy"]

        mem = get("/api/memory?limit=50")
        check("记忆接口带 owners 和 legacy",
              isinstance(mem.get("owners"), list) and isinstance(mem.get("legacy"), int),
              str(mem.get("owners")))
        check("未归属在 owners 里单独一项、名字固定叫「未归属」",
              any(o["owner"] == "__legacy__" and o["name"] == "未归属" and o["count"] >= 1
                  for o in mem["owners"]),
              str([x["owner"] for x in mem["owners"]]))
        check("★ 未归属那项排在最后面（它不是「某个人」）",
              mem["owners"][-1]["owner"] == "__legacy__", str(mem["owners"][-1]))
        check("每行都带 owner / owner_name / scope / chat_id",
              all(k in mem["items"][0] for k in ("owner", "owner_name", "scope", "chat_id")),
              str(sorted(mem["items"][0].keys())))

        only_legacy = get("/api/memory?owner=__legacy__&limit=50")["items"]
        check("★ 只看未归属时，返回的全是 owner 空串（不漏别人的）",
              bool(only_legacy) and all(x["owner"] == "" for x in only_legacy),
              f"{len(only_legacy)} 条")
        # 上面那句是「正例」，光有它还不够：如果库里此刻**只有**无主记忆，
        # 怎么返回都成立，断言等于空转。所以先塞一条有主的，再验它不在结果里。
        # （变异测试试过：把筛选去掉，这条没红之前，那句「全是空串」确实白验了。）
        bot.store.put("这条是有主人的记忆", owner=who, scope="private")
        only_legacy = get("/api/memory?owner=__legacy__&limit=50")["items"]
        check("★ 只看未归属时，有主的那条不在里面（反例）",
              bool(only_legacy) and not any("有主人的记忆" in x["text"]
                                            for x in only_legacy),
              f"{len(only_legacy)} 条")
        others = get("/api/memory?owner=" + who + "&limit=50")["items"]
        check("按具体归属筛选时，返回的全是那个人的（反例同验）",
              bool(others) and all(x["owner"] == who for x in others), f"{len(others)} 条")

        # 指派：只填 wxid 也认 —— 面板那个 prompt 就是让人手填 wxid 的，
        # 要求用户自己加 u: 前缀纯属为难人，所以前缀由服务端补。
        active_before = get("/api/memory?limit=10")["stats"]["active"]
        code, data = post("/api/memory/assign", {"id": legacy_id, "owner": "wxid_demo_owner"})
        check("指派成功，且只填 wxid 也认（服务端补 u: 前缀）",
              code == 200 and data.get("ok") and data.get("owner") == who,
              str(data.get("error") or data.get("owner")))
        new_id = data.get("id") or ""
        active_after = get("/api/memory?limit=10")["stats"]["active"]
        check("★ 指派是「换归属」不是「多一条」：有效条数不变",
              active_after == active_before, f"{active_before} → {active_after}")
        check("指派后 legacy 少一条",
              get("/api/memory?limit=10")["legacy"] == legacy_before - 1,
              f"{legacy_before} → {get('/api/memory?limit=10')['legacy']}")
        mine = get("/api/memory?owner=" + who + "&limit=50")["items"]
        check("按新归属能查回来，且 owner_name 不是空串",
              any(x["id"] == new_id for x in mine) and bool(mine[0]["owner_name"]),
              str([x["owner_name"] for x in mine]))

        # 最关键的一条：字段变了不算活，检索得到才算活了。
        # 这条如果不过，面板上的「指派」就只是个装饰品。
        # 用「我的生日」当 query：它是一句真话能命中的自然问法，也带字面重合，
        # 不会像纯语义查询那样卡在证据门槛边上而变成「看心情过不过」。
        hits = bot.retriever.search("我的生日", owner=who, scope="private")
        check("★ 指派之后那个人真的能检索到这条（不只是字段好看）",
              any("生日" in h.memory.text for h in hits),
              " | ".join(h.memory.text for h in hits) or "空")

        # 重启回放。本库是「追加日志 + 快照」，任何状态变化都必须落盘：
        # 只改内存字段的话，面板上点一次看着生效了，重启之后这条又变回未归属，
        # 用户会以为「指派」是骗人的。所以照「重开一次库」的方式验，不看内存态。
        replay = MemoryStore(Path(cfg.data_dir) / "memory", embedder=bot.embedder)
        check("★ 重启回放之后归属还在（指派是真的落盘了，不是改内存）",
              any(m.owner == who and "生日" in m.text
                  for m in replay.active(owner=who)),
              str(replay.owner_counts()))
        check("重启回放下旧记录是作废态，且 superseded_by 指向新记录",
              any(m.status == "void" and m.superseded_by == new_id
                  for m in [replay.get(legacy_id)] if m is not None),
              f"old={replay.get(legacy_id) and replay.get(legacy_id).status}")

        # 非法归属：指派的目的就是「让它有主」，所以空串不收；冒号后面没东西也不收。
        # 「abc」这种裸 wxid 是**合法**的（服务端补 u:），下面单独验它。
        code, data = post("/api/memory/assign", {"id": new_id, "owner": ""})
        check("空归属被拒（指派就是要给它一个主人）", code == 400, str(data.get("error")))
        code, data = post("/api/memory/assign", {"id": new_id, "owner": "u:"})
        check("冒号后为空被拒（会变成谁都查不到的归属）", code == 400, str(data.get("error")))
        code, data = post("/api/memory/assign", {"id": new_id, "owner": "x:wxid_bad"})
        check("前缀不是 u/g 被拒", code == 400, str(data.get("error")))
        code, data = post("/api/memory/assign", {"id": "不存在的id", "owner": who})
        check("指派不存在的 id 返回 400", code == 400, str(data.get("error")))
        # 跨群隔离的红线：scope=group 却拿不出 chat_id 的记录是「半截记录」——
        # 它既不属于任何群、又会被当成群记忆统计进去。旧记录是私聊（chat_id 空），
        # 这里主动要 group 却不给 chat_id，必须拒，不能默默继承出一个空 chat_id。
        priv = bot.store.put("私聊里的一句话", owner="", scope="private")
        code, data = post("/api/memory/assign",
                          {"id": priv["memory"].id, "owner": who, "scope": "group"})
        check("★ 指派成群记忆却给不出群 id：拒（不留半截记录）",
              code == 400, str(data.get("error")))

        code, data = post("/api/memory/void", {"id": new_id})
        check("先作废掉刚指派的那条", code == 200 and data.get("ok"), str(data.get("error")))
        code, data = post("/api/memory/assign", {"id": new_id, "owner": who})
        # 作废是「留档但不复活」（见面板那句提示）。如果这里能指派成功，
        # 用户会以为作废是可逆的，实际上是把一条该消失的记忆从坟里拉回来。
        check("作废之后再指派返回 400（作废不复活）", code == 400, str(data.get("error")))

        kb = get("/api/kb")
        check("资料库接口能读", kb["ok"] and "docs" in kb, str(kb["stats"]))
        tools = get("/api/tools")
        names = [t["name"] for t in tools["items"]]
        check("工具清单有 now / web_search", "now" in names and "web_search" in names, str(names))
        check("工具带说明与参数", all(t["description"] for t in tools["items"]))

        sysinfo = get("/api/system")
        check("系统信息带路径", sysinfo["ok"] and sysinfo["root"] and sysinfo["python"],
              sysinfo["python"])
        check("依赖版本能查", sysinfo["deps"].get("fastapi", "没装") != "没装",
              str(sysinfo["deps"].get("fastapi")))
        r = requests.get(BASE + "/api/voice/nope.mp3", timeout=5)
        check("拿不存在的音频返回 404", r.status_code == 404, str(r.status_code))

        # ---------------------------------------------------------- 13. 清理与日志环
        print("\n13. 收尾与日志环")
        code, data = post("/api/personas/delete", {"name": "演示角色"})
        check("删角色", code == 200, str(data.get("error")))
        check("删角色会清掉指向它的会话绑定", data.get("cleared") == 1, str(data.get("cleared")))
        check("默认角色没被带崩", get("/api/personas")["default"] != "演示角色")
        check("被删角色的会话回到默认人设",
              not bot.persona_for(test_chat).startswith("你是演示角色"))
        post("/api/chats/forget", {"id": test_chat})
        check("清掉演示会话的策略", not get("/api/chats")["items"] or
              all(x["id"] != test_chat for x in get("/api/chats")["items"]))
        code, data = post("/api/personas/delete", {"name": "演示角色"})
        check("删不存在的角色返回 400", code == 400, str(data.get("error")))

        logging.getLogger("demo").info("这是演示日志 %s", time.strftime("%H:%M:%S"))
        logs = get("/api/status")["logs"]
        check("日志进了环里", any("这是演示日志" in x["msg"] for x in logs), f"{len(logs)} 条")
    finally:
        monitor.stop(server)
        detach_log_handler(handler)
        # 先放掉 SQLite 连接，否则 Windows 上文件被占，临时目录删不干净
        try:
            if getattr(bot, "kb", None) is not None:
                bot.kb.close()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILS:
        print(f"有 {len(FAILS)} 项没过：" + "、".join(FAILS))
        return 1
    print("后台面板可用：页面、改参数、恢复默认、动作、日志都通。")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sys.exit(main())
