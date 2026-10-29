# deepChat v1

一个跑在 Windows 上的微信陪聊机器人。收消息读本地微信数据库（不碰界面），
发消息用 UIA 驱动微信客户端（没法子，PC 端没有开放 API）。

有长期记忆（语义检索 + 查重合并 + 容量淘汰）、按人/按群的说话策略、
群聊接话打分、本地资料库检索、联网搜索、天气、语音，以及一个本机后台面板。

**这不是一个通用框架。** 它是围绕「一个人用、一台机器、一个微信号」写的，
很多决定是为了避开具体的坑（微信把窗口最小化就发不出去、向量模型要能离线跑、
面板改参数要立刻生效），理由都写在对应模块的 docstring 里，改之前先读那段。

---

## 目录

```
bot02/
├── start.bat            双击入口 → run.py 菜单
├── run.py               命令行入口（run / selftest / doctor / index / ask / web）
├── config.yaml          行为配置（带注释，程序从不回写）
├── requirements.txt     依赖
├── .env.example         密钥模板，复制成 .env 再填
├── app/                 源码
│   ├── bot.py           编排：一条消息从进来到出去
│   ├── wechat/          微信接入（bridge 收发 / reply_split 切分）
│   ├── message_tagging.py   打标：值不值得记、什么类型
│   ├── message_content.py   原始消息 → 能进模型的文本（XML 折叠 / 非文本占位）
│   ├── embeddings.py    本地向量（bge-small-zh）
│   ├── memory.py        记忆存储（追加日志 + 快照 + 淘汰）
│   ├── dedup_judge.py   查重灰区的模型裁判
│   ├── retrieval.py     检索（相似度 0.7 + 重要度 0.3）
│   ├── context_budget.py    闸门：唯一能往上下文塞东西的地方
│   ├── llm.py           模型调用（重试 / 空回复放大 / 工具调用）
│   ├── sender.py        发送层：切分 / 冷却 / 重试 / 语音
│   ├── chat_locks.py    按会话发锁（引用计数，归零才淘汰）
│   ├── chat_policies.py 按会话的监听策略
│   ├── user_levels.py   用户等级（owner / friend / stranger / blocked）
│   ├── group_engage.py  群聊接话打分
│   ├── group_log.py     关键用户群消息流水
│   ├── personas.py      多角色库
│   ├── kb_store.py      资料库 SQLite（FTS5 + bm25）
│   ├── tools/           模型可调用的工具（web / weather / kb / history / registry）
│   ├── voice/           语音合成
│   └── panel/           后台面板（state / schema / server / page + static/index.html）
├── tests/               十一个回归脚本 + run_all.py
├── prompts/personas/    人设（一个角色一个 md，文件名即角色名）
├── knowledge/           给机器人查的资料（md / txt / pdf / docx）
├── docs/                项目文档（不参与索引）
│   ├── guide/           使用说明
│   ├── design/          实现方案、需求规格、方案设计
│   └── history/         交接记录（按时间倒序的工作日志）
├── vendor/winsdk/       Python 3.13 的 OCR 垫片
└── data/                运行产物（不入库）：日志、记忆库、索引、语音缓存
```

**`knowledge/` 和 `docs/` 是两回事**：前者是喂给机器人、聊天时会被引用的资料，
后者是给人看的开发文档。混在一起，机器人就会拿设计文档去回用户的话。

---

## 安装

需要 **Python 3.13**（3.12 也行；3.13 是因为要配 `winrt-*` 那套 OCR）。

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

有两条不能省的特殊处理，原因写在 `requirements.txt` 末尾：

1. `wechatauto-replica` 必须 **`--no-deps` 单独装**（它硬性要求 `winsdk>=1.0.0b10`，
   而 winsdk 最高只到 cp312，硬装会连累整个环境）：
   ```bat
   .venv\Scripts\python.exe -m pip install --no-deps wechatauto-replica
   ```
2. Python 3.13 没有可用的 winsdk 轮子，走 `winrt-*` + `vendor/winsdk` 垫片，
   两者缺一不可。

装完自检：

```bat
.venv\Scripts\python.exe run.py doctor
```

然后复制 `.env.example` 为 `.env`，填 `DEEPSEEK_API_KEY`（唯一必需项）。

---

## 跑

```bat
start.bat
```

菜单里选：启动机器人 / 发送自检 / 环境自检 / 建资料索引 / 只开面板。
直接回车 = 启动，窗口别关，后台面板跟着起在 <http://127.0.0.1:8787>。

命令行等价写法见 [`docs/guide/使用说明.md`](docs/guide/使用说明.md)。

---

## 测试

十一个回归脚本，互相独立，全部跑完约 50 秒。改核心代码后跑一遍：

```bat
.venv\Scripts\python.exe tests\run_all.py
.venv\Scripts\python.exe tests\run_all.py memory    :: 只跑名字含 memory 的
```

| 脚本 | 验什么 |
|---|---|
| `test_context_budget.py` | 闸门预算回砍、回复切分、重复消息 |
| `test_memory.py` | 记忆写入、检索门槛、查重合并、容量淘汰 |
| `test_identity_isolation.py` | 别人的记忆一条都捞不到 |
| `test_message_content.py` | XML 折叠 / 非文本占位：不污染上下文、不静默丢弃 |
| `test_concurrency.py` | 并发：同会话串行 / 跨会话并行 / 指纹恰好一次 |
| `test_tool_loop.py` | 工具循环：正常 / 预算截断 / 轮次用尽 / 坏输入 |
| `test_group_engage.py` | 群聊接话打分：被叫到就回、不插嘴 |
| `test_group_log.py` | 关键用户群消息记录（只追加流水） |
| `test_panel.py` | 后台面板：接口、改参数、恢复默认、重建索引 |
| `test_send_guard.py` | 发送护栏：同一条回复不会被发两遍 |
| `test_integration.py` | 端到端：真 Bot + 假微信 + 假模型 |

全部不联网、不动真实的 `data/`（各自在临时目录里造环境，跑完删掉）。
共用的断言计数与分隔线在 `tests/_support.py`。

---

## 文档

| 想知道什么 | 看哪 |
|---|---|
| 怎么用、参数什么意思、出问题怎么排查 | [`docs/guide/使用说明.md`](docs/guide/使用说明.md) |
| 为什么这么设计、踩过哪些坑 | 各模块 docstring（写得比文档细），以及 `docs/history/交接记录.md` |
| 接下来要做什么 | [`docs/design/`](docs/design) 下的需求与方案 |
| 一开始的分期规划 | [`docs/design/实现方案.md`](docs/design/实现方案.md) |

---

## 关于 2026-10-29 的这次整理

整理前仓库根目录是「新旧两版并放的容器」：`新版/`（在跑的）、`旧版-20261026/`（旧架构快照）、
`_回退-20261026-2004/`（回退点），外加九个 `演示-*.py` 平铺在 `tests/` 里。

做了什么：

- 删掉两个历史副本（已打包到仓库外的 `bot02-旧版归档-20261029.zip`），把 `新版/` 提升为仓库根；
- 项目文档收进 `docs/`，机器人语料从 `docs/` 移到 `knowledge/`，两者不再混用；
- 模块改成看得懂的名字，九个测试脚本改英文名并抽出重复了八遍的脚手架；
- 补齐根 `.gitignore`、`README.md`，`启动.bat` → `start.bat`。

改名对照（旧 → 新），给翻旧记录用：

| 旧 | 新 |
|---|---|
| `gate.py` / `Gate` | `context_budget.py` / `ContextBudget` |
| `judge.py` / `LlmJudge` | `dedup_judge.py` / `DedupJudge` |
| `people.py` / `PeopleStore` | `user_levels.py` / `UserLevelStore` |
| `engage.py` / `Engager` | `group_engage.py` / `GroupEngager` |
| `retrieve.py` / `Retriever` | `retrieval.py` / `MemoryRetriever` |
| `tagging.py` | `message_tagging.py` |
| `chats.py` | `chat_policies.py` |
| `grouplog.py` | `group_log.py` |
| `embed.py` | `embeddings.py` |
| `kbstore.py` | `kb_store.py` |
| `control/` | `panel/` |
| `tools/base.py` | `tools/registry.py` |
| `wechat/split.py` | `wechat/reply_split.py` |
