# -*- coding: utf-8 -*-
"""后台面板的页面（单文件，样式脚本全内嵌）。

版式照旧版的配置编辑器来：左边分区导航、顶栏放全局操作、右边一屏一块白面板。
前端不引框架、不引 CDN、不联网——断网也照常打开，改完重启就看到。

三件事必须记住：
  1. 面板每 2 秒拉一次状态；**参数输入框不能被刷新冲掉**（用户正在打字）。
  2. 每个分区只在自己被打开时去拉数据（记忆库、资料库、日志量都不小）。
  3. 所有来自后端的文本都要 esc() 一遍再进 innerHTML。
"""

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>deepChat · 后台</title>
<style>
  :root{
    --green:#4caf50; --green-dark:#43a047; --green-soft:#e8f5e9; --green-ink:#2e7d32;
    --blue:#2196f3; --orange:#ff9800; --red:#f44336;
    --bg:#f7f8fc; --panel:#fff; --line:#e6e8ee; --line2:#f0f2f5;
    --text:#1f2937; --muted:#6b7280; --faint:#9aa3b2;
    --radius:8px; --shadow:0 2px 8px rgba(0,0,0,.08);
    --mono:ui-monospace,SFMono-Regular,"Cascadia Mono",Consolas,monospace;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0;background:var(--bg);color:var(--text);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased;display:flex;flex-direction:column;overflow:hidden;
  }
  /* ---------------------------------------------------- 顶栏 */
  .topbar{
    display:flex;align-items:center;gap:14px;padding:0 20px;height:56px;flex:none;
    background:#fff;border-bottom:1px solid var(--line);box-shadow:0 1px 3px rgba(0,0,0,.05);
    z-index:20;
  }
  .brand{font-size:17px;font-weight:650;letter-spacing:.2px;white-space:nowrap}
  .brand span{color:var(--faint);font-weight:400;font-size:13px;margin-left:6px}
  .statusline{display:flex;align-items:center;gap:14px;font-size:12.5px;color:var(--muted);
              flex:1;min-width:0;overflow:hidden;white-space:nowrap}
  .statusline b{color:#374151;font-weight:600}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;
        font-size:12px;font-weight:600;border:1px solid transparent}
  .pill.on{background:var(--green-soft);color:var(--green-ink);border-color:#c8e6c9}
  .pill.off{background:#f3f4f6;color:var(--muted);border-color:var(--line)}
  .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
  .pill.on .dot{box-shadow:0 0 0 3px rgba(76,175,80,.18)}
  .topbar .acts{display:flex;gap:8px;flex:none}

  button{font:inherit;cursor:pointer;border-radius:6px;padding:7px 14px;font-size:13px;
         border:1px solid var(--line);background:#fff;color:#374151;transition:.15s}
  button:hover{border-color:#cfd6e4;background:#fafbff}
  button:active{transform:translateY(1px)}
  button:disabled{opacity:.5;cursor:not-allowed;transform:none}
  button.green{background:var(--green);border-color:var(--green);color:#fff}
  button.green:hover{background:var(--green-dark);border-color:var(--green-dark)}
  button.blue{background:var(--blue);border-color:var(--blue);color:#fff}
  button.blue:hover{background:#1e88e5}
  button.orange{background:var(--orange);border-color:var(--orange);color:#fff}
  button.orange:hover{background:#fb8c00}
  button.red{background:var(--red);border-color:var(--red);color:#fff}
  button.red:hover{background:#e53935}
  button.sm{padding:4px 10px;font-size:12px;border-radius:5px}
  button.ghost{color:var(--muted)}

  /* ---------------------------------------------------- 布局 */
  .shell{display:flex;flex:1;min-height:0}
  .sidebar{
    width:212px;flex:none;background:#fff;border-right:1px solid var(--line);
    overflow-y:auto;padding:10px 0;box-shadow:1px 0 3px rgba(0,0,0,.03);
  }
  .sidebar a{
    display:flex;align-items:center;justify-content:space-between;gap:8px;
    padding:10px 18px;color:#444;text-decoration:none;font-size:13.5px;
    border-left:4px solid transparent;cursor:pointer;
  }
  .sidebar a:hover{background:#f5f6f9;color:#222}
  .sidebar a.active{background:var(--green-soft);color:var(--green-ink);
                    font-weight:600;border-left-color:var(--green)}
  .sidebar a .n{font-size:11px;color:#fff;background:var(--orange);
                border-radius:99px;padding:0 6px;line-height:16px}
  .sidebar .sep{height:1px;background:var(--line2);margin:8px 16px}
  .content{flex:1;min-width:0;overflow-y:auto;padding:20px 22px 60px}

  .panel{display:none}
  .panel.on{display:block}
  .card{background:var(--panel);border-radius:var(--radius);box-shadow:var(--shadow);
        padding:20px 22px;margin-bottom:18px}
  .card > h2{
    margin:0 0 18px;font-size:18px;font-weight:600;color:#333;
    border-bottom:1px solid #eee;padding-bottom:10px;
    display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
  }
  .card > h2 .tools{display:flex;gap:8px;font-size:13px;font-weight:400}
  .card .desc{color:var(--muted);font-size:12.5px;margin:-8px 0 16px}
  h3.sub{font-size:14px;font-weight:600;color:#444;margin:22px 0 10px}

  .alert{background:#fff8e1;border:1px solid #ffe0a3;color:#8a5300;border-radius:8px;
         padding:10px 14px;margin-bottom:14px;font-size:13px;line-height:1.6}

  /* ---------------------------------------------------- KPI */
  .kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
  @media (max-width:1100px){.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
  @media (max-width:820px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
  .kpi{border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px;background:#fff}
  .kpi .k{font-size:12px;color:var(--muted)}
  .kpi .v{font-size:21px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
  .kpi .v small{font-size:11.5px;font-weight:500;color:var(--faint);margin-left:3px}
  .kpi .t{font-size:11.5px;color:var(--faint);margin-top:5px;min-height:17px}
  .kpi.bad{border-color:#ffcdd2;background:#fffafa}
  .kpi.bad .v{color:var(--red)}

  /* ---------------------------------------------------- 表格 */
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;font-weight:600;color:var(--muted);font-size:12px;
     background:#fafbfc;padding:9px 10px;border-bottom:1px solid var(--line)}
  td{padding:9px 10px;border-bottom:1px solid var(--line2);vertical-align:top}
  tbody tr:hover{background:#fcfdff}
  tr:last-child td{border-bottom:none}
  .tbl-wrap{border:1px solid var(--line);border-radius:var(--radius);overflow:auto;max-height:420px}
  .mono{font-family:var(--mono);font-size:12px;color:var(--muted);white-space:nowrap}
  .clip{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:340px}
  .empty{color:var(--faint);text-align:center;padding:26px 10px;font-size:13px}
  .badge{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11.5px;
         background:#f3f4f6;color:var(--muted);border:1px solid var(--line);white-space:nowrap}
  .badge.acc{background:#e3f2fd;color:#1565c0;border-color:#bbdefb}
  .badge.warn{background:#fff8e1;color:#b26a00;border-color:#ffe0a3}
  .badge.err{background:#ffebee;color:#c62828;border-color:#ffcdd2}
  .badge.ok{background:var(--green-soft);color:var(--green-ink);border-color:#c8e6c9}

  /* ---------------------------------------------------- 参数表单 */
  .grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 26px}
  @media (max-width:1000px){.grid2{grid-template-columns:1fr}}
  .field{padding:12px 0;border-bottom:1px dashed var(--line2)}
  .field:last-child{border-bottom:none}
  .flabel{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:13.5px;font-weight:600}
  .fpath{font-family:var(--mono);font-size:11px;color:var(--faint);font-weight:400}
  .frow{display:flex;align-items:center;gap:6px;margin-top:7px}
  .frow input[type=text],.frow input[type=number],.frow select{
    flex:1;min-width:0;height:34px;padding:0 10px;border:1px solid var(--line);
    border-radius:6px;font-size:13.5px;font-family:inherit;color:var(--text);
    background:#fff;outline:none;transition:.15s;
  }
  .frow input:focus,.frow select:focus{border-color:var(--green);
    box-shadow:0 0 0 3px rgba(76,175,80,.14)}
  .frow input[type=number]{font-family:var(--mono)}
  .frow .suffix{color:var(--faint);font-size:12px;min-width:30px}
  .fhint{color:var(--faint);font-size:12px;margin-top:6px;line-height:1.5}
  .fhint b{color:var(--muted);font-weight:600}

  .switch{position:relative;display:inline-block;width:42px;height:23px;flex:none}
  .switch input{opacity:0;width:0;height:0;position:absolute}
  .switch span{position:absolute;inset:0;background:#d5d9e0;border-radius:99px;
               transition:.16s;cursor:pointer}
  .switch span:before{content:"";position:absolute;width:17px;height:17px;left:3px;top:3px;
    background:#fff;border-radius:50%;transition:.16s;box-shadow:0 1px 2px rgba(0,0,0,.25)}
  .switch input:checked + span{background:var(--green)}
  .switch input:checked + span:before{transform:translateX(19px)}

  /* ---------------------------------------------------- 工具条 */
  .toolrow{display:grid;grid-template-columns:minmax(80px,1.2fr) 1fr 96px;
           gap:12px;align-items:center;padding:7px 0;font-size:13px}
  .toolrow .name{font-family:var(--mono);font-size:12.5px;color:#374151;
                 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bar{height:7px;border-radius:99px;background:#f0f2f5;overflow:hidden}
  .bar i{display:block;height:100%;border-radius:99px;background:var(--green)}
  .toolrow .num{text-align:right;font-family:var(--mono);font-size:11.5px;color:var(--muted)}
  .toolrow .num .f{color:var(--red)}

  /* ---------------------------------------------------- 日志 */
  .logbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
  .logbar input[type=text]{flex:1;min-width:160px;height:34px;padding:0 10px;
    border:1px solid var(--line);border-radius:6px;font-size:13px;outline:none}
  .logbar input[type=text]:focus{border-color:var(--green);box-shadow:0 0 0 3px rgba(76,175,80,.14)}
  .logbar select{height:34px;border:1px solid var(--line);border-radius:6px;padding:0 8px;
    font-size:13px;background:#fff;outline:none}
  .logs{font-family:var(--mono);font-size:12px;line-height:1.7;max-height:520px;overflow:auto;
        background:#fbfcfe;border:1px solid var(--line);border-radius:var(--radius);padding:10px 14px}
  .log{display:grid;grid-template-columns:58px 52px 108px 1fr;gap:10px;white-space:pre-wrap;
       word-break:break-word;padding:1px 0}
  .log .lv{font-weight:600}
  .log.INFO .lv{color:var(--muted)}
  .log.DEBUG .lv{color:var(--faint)}
  .log.WARNING .lv{color:#b26a00}
  .log.ERROR .lv,.log.CRITICAL .lv{color:var(--red)}
  .log .src{color:var(--faint);overflow:hidden;text-overflow:ellipsis}

  /* ---------------------------------------------------- 角色库 */
  .persona-wrap{display:grid;grid-template-columns:232px 1fr;gap:18px}
  @media (max-width:1000px){.persona-wrap{grid-template-columns:1fr}}
  .p-list{border:1px solid var(--line);border-radius:var(--radius);overflow:auto;max-height:430px}
  .p-item{display:flex;justify-content:space-between;align-items:center;gap:8px;
          padding:10px 12px;border-bottom:1px solid var(--line2);cursor:pointer;font-size:13px}
  .p-item:hover{background:#f7f9fc}
  .p-item.on{background:var(--green-soft);color:var(--green-ink);font-weight:600}
  .p-item:last-child{border-bottom:none}
  .p-item .meta{color:var(--faint);font-size:11.5px;font-weight:400;white-space:nowrap}
  select.cell{height:30px;border:1px solid var(--line);border-radius:5px;font-size:12.5px;
              background:#fff;padding:0 6px;max-width:160px}

  /* ---------------------------------------------------- 弹窗 */
  .modal{position:fixed;inset:0;background:rgba(17,24,39,.38);display:none;
         align-items:center;justify-content:center;z-index:60}
  .modal.on{display:flex}
  .modal-box{background:#fff;border-radius:10px;width:540px;max-width:92vw;max-height:82vh;
             display:flex;flex-direction:column;gap:10px;padding:20px 22px;
             box-shadow:0 12px 40px rgba(0,0,0,.22)}
  .modal-box h3{margin:0;font-size:16px;font-weight:600}
  .modal-box .who{color:var(--faint);font-size:12px}
  .modal-box .list{border:1px solid var(--line);border-radius:8px;overflow:auto;max-height:300px}
  .modal-box .person{display:flex;align-items:center;gap:8px;padding:8px 12px;
                     border-bottom:1px solid var(--line2);font-size:13px}
  .modal-box .person:last-child{border-bottom:none}
  .modal-box .person .meta{margin-left:auto;color:var(--faint);font-size:11.5px}
  .modal-box input[type=text]{height:34px;padding:0 10px;border:1px solid var(--line);
                              border-radius:6px;font:inherit;outline:none}
  .modal-box input[type=text]:focus{border-color:var(--green);box-shadow:0 0 0 3px rgba(76,175,80,.14)}
  .radio-row{display:flex;gap:14px;flex-wrap:wrap;font-size:13px}
  .radio-row label{display:flex;align-items:center;gap:5px;cursor:pointer}

  /* ---------------------------------------------------- 其它 */
  textarea{width:100%;min-height:340px;padding:12px 14px;border:1px solid var(--line);
    border-radius:6px;font-family:var(--mono);font-size:13px;line-height:1.7;outline:none;
    resize:vertical;color:#1f2937;background:#fdfefe}
  textarea:focus{border-color:var(--green);box-shadow:0 0 0 3px rgba(76,175,80,.14)}
  .kv{display:grid;grid-template-columns:178px 1fr;gap:9px 18px;font-size:13px}
  .kv .k{color:var(--muted)}
  .kv .v{font-family:var(--mono);font-size:12.5px;word-break:break-all}
  /* 参数行里的按钮跟输入框同高，不然一排高低不齐看着别扭 */
  .frow button{height:34px;padding:0 12px}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .spacer{flex:1}
  .note{color:var(--faint);font-size:12px;margin-top:10px}
  audio{height:34px;vertical-align:middle}

  #toast{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;gap:8px;z-index:99}
  .toast{background:#263238;color:#fff;padding:10px 16px;border-radius:8px;font-size:13px;
    box-shadow:0 6px 20px rgba(0,0,0,.2);opacity:0;transform:translateY(6px);transition:.18s;
    max-width:420px}
  .toast.show{opacity:1;transform:none}
  .toast.err{background:#c62828}
  .toast.ok{background:#2e7d32}
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">deepChat<span>后台</span></div>
  <div class="statusline">
    <span id="runpill" class="pill off"><i class="dot"></i><span>未运行</span></span>
    <span>账号 <b id="acct">—</b></span>
    <span>运行 <b id="uptime">—</b></span>
    <span>端口 <b id="addr">—</b></span>
    <span class="spacer"></span>
    <span>刷新 <b id="tick">—</b></span>
  </div>
  <div class="acts">
    <button class="blue" id="btnReindexTop">重建索引</button>
    <button class="ghost" id="btnResetAllTop">全部恢复默认</button>
  </div>
</div>

<div class="shell">
  <nav class="sidebar" id="nav"></nav>
  <main class="content" id="content">

    <!-- ============================================ 总览 -->
    <section class="panel" id="p-overview">
      <div class="card">
        <h2>运行总览 <span class="tools" id="ov-sub"></span></h2>
        <div id="ovAlert"></div>
        <div class="kpis" id="kpis"></div>
        <div class="note">每 2 秒自动刷新。数字是本进程启动以来的累计，重启归零。</div>
      </div>
      <div class="card">
        <h2>最近消息 <span class="tools"><span id="recentCount" class="badge"></span></span></h2>
        <div class="tbl-wrap">
          <table>
            <thead><tr>
              <th style="width:64px">时间</th><th style="width:110px">会话</th>
              <th>收到</th><th>回复</th>
              <th style="width:64px">耗时</th><th style="width:110px">工具</th>
            </tr></thead>
            <tbody id="recentBody"><tr><td colspan="6" class="empty">还没有消息</td></tr></tbody>
          </table>
        </div>
      </div>
      <div class="card">
        <h2>工具调用 <span class="tools">本进程累计</span></h2>
        <div id="ov-tools"><div class="empty">还没调过工具</div></div>
      </div>
    </section>

    <!-- ============================================ 人设（角色库） -->
    <section class="panel" id="p-persona">
      <div class="card">
        <h2>角色库
          <span class="tools">
            <button class="blue sm" id="btnPersonaNew">新建角色</button>
          </span>
        </h2>
        <p class="desc">每个角色是一份 md，文件名就是角色名。默认角色给「没单独指定的会话」用；
          想让某个群/某个人换性格，去「会话监视」里给那个会话绑定角色。</p>
        <div class="persona-wrap">
          <div class="p-list" id="personaList"><div class="empty">加载中…</div></div>
          <div>
            <div class="row" style="margin-bottom:10px">
              <span class="flabel">角色名</span>
              <input type="text" id="personaName" readonly title="改名用右边那个按钮" style="height:34px;padding:0 10px;border:1px solid var(--line);border-radius:6px;font:inherit;width:200px;background:#f8f9fb;color:#374151">
              <span class="badge" id="personaDefaultBadge">默认</span>
              <span class="spacer"></span>
              <button class="ghost sm" id="btnPersonaRename">改名</button>
              <button class="ghost sm" id="btnPersonaDefault">设为默认</button>
              <button class="red sm" id="btnPersonaDelete">删除</button>
              <button class="green sm" id="btnPersonaSave">保存并生效</button>
            </div>
            <textarea id="personaText" spellcheck="false"></textarea>
            <div class="note" id="personaMeta"></div>
          </div>
        </div>
      </div>
    </section>

    <!-- ============================================ 会话监视 -->
    <section class="panel" id="p-chats">
      <div class="card">
        <h2>会话监视
          <span class="tools"><button class="ghost sm" id="btnChatsRefresh">刷新会话</button></span>
        </h2>
        <p class="desc">每个群、每个人单独设：正常回 / 只在 @ 时回 / 只看不回 / 完全不看，
          还能给这个会话指定一个角色。改完<b>下一条消息就生效</b>，不用重启。</p>
        <div id="chatsHint"></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr>
              <th style="width:210px">会话</th><th style="width:56px">类型</th>
              <th style="width:140px">怎么回</th><th style="width:140px">用哪个角色</th>
              <th style="width:110px">成员</th>
              <th>最近一条</th><th style="width:76px"></th>
            </tr></thead>
            <tbody id="chatsBody"><tr><td colspan="7" class="empty">打开本区后才会读取</td></tr></tbody>
          </table>
        </div>
        <div class="note">会话列表来自微信本地库，只读不打扰。<b>成员</b>那一列可以只理某些人、
          或者不理某些人（按昵称或 wxid 认，改了立刻生效）。这里的设置存在
          <code>data/chats.json</code>，删掉该文件就全部回到默认策略。</div>
      </div>
    </section>

    <!-- ============================================ 用户等级与接话 -->
    <section class="panel" id="p-people">
      <div class="card">
        <h2>用户等级与接话 <span class="tools"><button class="ghost sm" data-reset-group="people">本区恢复默认</button></span></h2>
        <p class="desc">接话打分只对「主人 / 熟人」开放主动接话；陌生人只能被 @ 或叫到才回。填 wxid 最稳。</p>
        <div class="grid2" data-fields="people"></div>
      </div>
      <div class="card">
        <h2>已设等级的人 <span class="tools"><button class="ghost sm" id="btnPeopleRefresh">刷新</button></span></h2>
        <div class="tbl-wrap"><table>
          <thead><tr><th>wxid</th><th style="width:80px">称呼</th><th style="width:90px">等级</th><th style="width:200px"></th></tr></thead>
          <tbody id="peopleBody"><tr><td colspan="4" class="empty">打开本区后才会读取</td></tr></tbody>
        </table></div>
        <div class="note">这里改的是 data/people.json，改完下一条消息就生效。想加新人先知道他的 wxid（群里 @ 他的记录里能看到）。</div>
      </div>
    </section>

    <!-- ============================================ 对话 -->
    <section class="panel" id="p-chat">
      <div class="card">
        <h2>对话与发送
          <span class="tools"><button class="ghost sm" data-reset-group="chat">本区恢复默认</button></span>
        </h2>
        <p class="desc">控制「怎么回、什么时候回、怎么发出去」。标记「需重启」的项改了要重启进程。</p>
        <div class="grid2" data-fields="chat"></div>
      </div>
    </section>

    <!-- ============================================ 模型 -->
    <section class="panel" id="p-llm">
      <div class="card">
        <h2>模型与工具调用
          <span class="tools"><button class="ghost sm" data-reset-group="llm">本区恢复默认</button></span>
        </h2>
        <p class="desc">温度、单次回复上限、工具循环的轮数与预算。改完下一句生效。</p>
        <div class="grid2" data-fields="llm"></div>
      </div>
    </section>

    <!-- ============================================ 记忆 -->
    <section class="panel" id="p-memory">
      <div class="card">
        <h2>记忆策略
          <span class="tools"><button class="ghost sm" data-reset-group="memory">本区恢复默认</button></span>
        </h2>
        <p class="desc">容量满了淘汰最没用的；证据门槛用来「宁可不捞也别硬凑」。</p>
        <div class="grid2" data-fields="memory"></div>
      </div>
      <div class="card">
        <h2>记忆库 <span class="tools"><span id="memStats" class="badge"></span>
          <button class="ghost sm" id="btnMemRefresh">刷新</button></span></h2>
        <div class="logbar">
          <input type="text" id="memQuery" placeholder="试试语义检索：输入一句话，看能捞到什么（不会写库、不算使用次数）">
          <button class="blue sm" id="btnMemSearch">检索</button>
        </div>
        <div id="memHits"></div>
        <!-- 归属筛选：旧数据（无归属）谁都查不到，等于永久失忆。这个下拉
             既是「找出还剩多少条待补救」的入口，也是指派完确认结果的入口。
             选项由接口的 owners 数组渲染，切换即重新拉取。 -->
        <div class="logbar" style="margin-top:10px">
          <select id="memOwner" style="max-width:240px"><option value="">全部归属</option></select>
        </div>
        <div class="tbl-wrap" style="margin-top:10px">
          <table>
            <thead><tr>
              <th>内容</th><th style="width:66px">类型</th><th style="width:60px">重要</th>
              <th style="width:70px">用过</th><th style="width:110px">归属</th>
              <th style="width:110px">写入</th>
              <th style="width:110px">上次用到</th><th style="width:120px"></th>
            </tr></thead>
            <tbody id="memBody"><tr><td colspan="8" class="empty">打开本区后才会读取</td></tr></tbody>
          </table>
        </div>
        <div class="note">作废 = 留档但不进检索，不会真的删除（追加日志里还能查到）。
          「未归属」是迁移前的旧数据：谁都查不到，点「指派」给它补一个主人就能复活。</div>
      </div>
    </section>

    <!-- ============================================ 联网 -->
    <section class="panel" id="p-search">
      <div class="card">
        <h2>联网搜索
          <span class="tools"><button class="ghost sm" data-reset-group="search">本区恢复默认</button></span>
        </h2>
        <p class="desc">web_search（Bing RSS→HTML 双路）、read_url、weather 都看这里。</p>
        <div class="grid2" data-fields="search"></div>
      </div>
    </section>

    <!-- ============================================ 资料库 -->
    <section class="panel" id="p-kb">
      <div class="card">
        <h2>资料库
          <span class="tools">
            <button class="ghost sm" data-reset-group="kb">本区恢复默认</button>
            <button class="orange sm" id="btnKbReindex">重建索引</button>
          </span>
        </h2>
        <p class="desc">资料库和记忆不是一回事：这里只放「用户自己的文档」，坏了重建就行。
          放资料到 <code>knowledge/</code> 之后点重建索引。</p>
        <div class="grid2" data-fields="kb"></div>
        <h3 class="sub">已索引文档 <span id="kbStats" class="badge"></span></h3>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>标题</th><th>路径</th><th style="width:80px">字数</th>
              <th style="width:60px">分块</th><th style="width:110px">更新</th></tr></thead>
            <tbody id="kbBody"><tr><td colspan="5" class="empty">打开本区后才会读取</td></tr></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- ============================================ 聊天记录 -->
    <section class="panel" id="p-history">
      <div class="card">
        <h2>聊天记录工具
          <span class="tools"><button class="ghost sm" data-reset-group="history">本区恢复默认</button></span>
        </h2>
        <p class="desc">让模型能翻微信聊天记录（只读本地消息库，不会给任何人发消息）。
          命令行 <code>ask</code> 模式没有接入层，这个工具不会挂。</p>
        <div class="grid2" data-fields="history"></div>
      </div>
    </section>

    <!-- ============================================ 语音 -->
    <section class="panel" id="p-voice">
      <div class="card">
        <h2>语音
          <span class="tools"><button class="ghost sm" data-reset-group="voice">本区恢复默认</button></span>
        </h2>
        <p class="desc">微信 4.x 发不了语音气泡，发出去的是 mp3 文件，对方点开就能听。</p>
        <div class="grid2" data-fields="voice"></div>
      </div>
      <div class="card">
        <h2>试听</h2>
        <div class="logbar">
          <input type="text" id="voiceText" placeholder="写一句话，用当前音色合成（要点网络）">
          <button class="green sm" id="btnVoiceTest">合成并试听</button>
        </div>
        <div id="voicePlayer"></div>
        <div class="note">合成结果按内容哈希缓存在 <code>data/voice/</code>，同一句不会重复请求。</div>
      </div>
    </section>

    <!-- ============================================ 工具 -->
    <section class="panel" id="p-tools">
      <div class="card">
        <h2>当前挂载的工具 <span class="tools"><button class="ghost sm" id="btnToolsRefresh">刷新</button></span></h2>
        <p class="desc">这是「按当前配置」能挂上的工具清单。关掉某个开关（联网 / 资料库 / 记忆 / 语音），
          对应工具会从这里消失。</p>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th style="width:130px">工具</th><th>做什么</th>
              <th style="width:180px">参数</th><th style="width:110px">本进程调用</th></tr></thead>
            <tbody id="toolsBody"><tr><td colspan="4" class="empty">打开本区后才会读取</td></tr></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- ============================================ 日志 -->
    <section class="panel" id="p-logs">
      <div class="card">
        <h2>程序运行日志
          <span class="tools">
            <label style="cursor:pointer;font-size:12.5px;color:var(--muted)">
              <input type="checkbox" id="logPause"> 暂停
            </label>
            <label style="cursor:pointer;font-size:12.5px;color:var(--muted)">
              <input type="checkbox" id="logAuto" checked> 自动滚到底
            </label>
          </span>
        </h2>
        <div class="logbar">
          <input type="text" id="logQuery" placeholder="按关键词过滤（工具名、会话、报错…）">
          <select id="logLevel">
            <option value="">全部级别</option>
            <option value="INFO">INFO 以上</option>
            <option value="WARNING">只看 WARNING</option>
            <option value="ERROR">只看 ERROR</option>
          </select>
          <span class="badge" id="logCount"></span>
        </div>
        <div class="logs" id="logs"><div class="empty">还没有日志</div></div>
        <div class="note">面板只留最近 200 条；完整日志在 <code>data/bot.log</code>（滚动 4MB×3）。</div>
      </div>
    </section>

    <!-- ============================================ 系统 -->
    <section class="panel" id="p-system">
      <div class="card">
        <h2>系统信息 <span class="tools"><button class="ghost sm" id="btnSysRefresh">刷新</button></span></h2>
        <div class="kv" id="sysKv"><div class="k">加载中…</div><div class="v"></div></div>
      </div>
      <div class="card">
        <h2>面板与运行参数
          <span class="tools"><button class="ghost sm" data-reset-group="system">本区恢复默认</button></span>
        </h2>
        <div class="grid2" data-fields="system"></div>
      </div>
      <div class="card">
        <h2>日志与维护</h2>
        <div class="grid2" data-fields="log"></div>
        <div class="row" style="margin-top:16px">
          <button class="ghost" id="btnSnapshot">记忆落盘快照</button>
          <button class="ghost" id="btnClearRecent">清空面板记录</button>
          <span class="note" id="maintNote" style="margin:0"></span>
        </div>
      </div>
      <div class="card">
        <h2>被覆盖的参数 <span class="tools"><span id="ovCount" class="badge"></span></span></h2>
        <div id="overrideList" class="note">没有改过任何参数，全部用的 config.yaml 默认值。</div>
      </div>
    </section>

  </main>
</div>

<div id="modal" class="modal">
  <div class="modal-box">
    <h3 id="modalTitle">谁说的话算数</h3>
    <div class="who" id="modalWho"></div>
    <div class="radio-row" id="modalModes"></div>
    <input type="text" id="modalAdd" placeholder="手动加一个人：昵称或 wxid，回车加入">
    <div class="list" id="modalList"></div>
    <div class="row" style="justify-content:flex-end">
      <button class="ghost" id="modalCancel">取消</button>
      <button class="green" id="modalSave">保存</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
/* =====================================================================
   状态与工具函数
   ===================================================================== */
const SECTIONS = [
  { id: "overview", title: "总览" },
  { id: "persona",  title: "角色库" },
  { id: "chats",    title: "会话监视" },
  { id: "people",   title: "用户等级", group: "people" },
  { id: "chat",     title: "对话与发送", group: "chat" },
  { id: "llm",      title: "模型与工具", group: "llm" },
  { id: "memory",   title: "记忆",       group: "memory" },
  { id: "search",   title: "联网",       group: "search" },
  { id: "kb",       title: "资料库",     group: "kb" },
  { id: "history",  title: "聊天记录",   group: "history" },
  { id: "voice",    title: "语音",       group: "voice" },
  { id: "tools",    title: "工具清单" },
  { id: "logs",     title: "日志" },
  { id: "system",   title: "系统" },
];

const S = {
  tab: "overview",
  fields: [],
  groups: [],
  rendered: false,
  dirty: new Set(),
  status: null,
  loaded: {},          // 分区数据是否已拉过
  lastLogKey: "",
  tabSig: "",
};

function $(id){ return document.getElementById(id); }
function esc(s){
  return String(s == null ? "" : s).replace(/[&<>"]/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];
  });
}
function toast(msg, kind){
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = msg;
  $("toast").appendChild(el);
  requestAnimationFrame(function(){ el.classList.add("show"); });
  setTimeout(function(){
    el.classList.remove("show");
    setTimeout(function(){ el.remove(); }, 250);
  }, 2800);
}
async function api(path, opts){
  const res = await fetch(path, Object.assign({headers:{"Content-Type":"application/json"}}, opts || {}));
  let data = null;
  try { data = await res.json(); } catch(e){ throw new Error("返回不是 JSON"); }
  if (!res.ok || data.ok === false) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}
function dur(sec){
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec/3600), m = Math.floor(sec%3600/60), s = sec%60;
  if (h) return h + " 小时 " + m + " 分";
  if (m) return m + " 分 " + s + " 秒";
  return s + " 秒";
}
function fmtMs(ms){
  if (ms == null) return "—";
  return ms < 1000 ? Math.round(ms) + "ms" : (ms/1000).toFixed(1) + "s";
}
function badgeOf(f){
  let out = "";
  if (f.overridden) out += '<span class="badge ok">已改</span>';
  if (f.restart) out += '<span class="badge warn">需重启</span>';
  return out;
}

/* =====================================================================
   左侧导航
   ===================================================================== */
function renderNav(n){
  const counts = {};
  S.fields.forEach(function(f){ if (f.overridden) counts[f.group] = (counts[f.group]||0)+1; });
  const html = SECTIONS.map(function(sec, i){
    const n2 = counts[sec.group] || 0;
    const sep = (i === 1 || sec.id === "tools") ? '<div class="sep"></div>' : "";
    return sep + '<a data-tab="' + sec.id + '" class="' + (S.tab === sec.id ? "active":"") + '">' +
      '<span>' + esc(sec.title) + '</span>' +
      (n2 ? '<span class="n">' + n2 + '</span>' : '') + '</a>';
  }).join("");
  $("nav").innerHTML = html;
  $("nav").querySelectorAll("[data-tab]").forEach(function(a){
    a.addEventListener("click", function(){ go(a.dataset.tab); });
  });
}
/* 这两个都可能抛：file:// 下 localStorage 和 history.replaceState 会被安全策略挡掉。
   挡掉不该让整个面板白屏，所以一律 try 一下，记不住上次分区无所谓。 */
function rememberTab(id){
  try{ localStorage.setItem("xiaoshen.tab", id); }catch(e){}
  try{
    if (location.hash.slice(1) !== id) history.replaceState(null, "", "#" + id);
  }catch(e){}
}
function savedTab(){
  try{ return localStorage.getItem("xiaoshen.tab"); }catch(e){ return null; }
}
function go(id){
  S.tab = id;
  rememberTab(id);
  SECTIONS.forEach(function(sec){
    const panel = $("p-" + sec.id);
    if (panel) panel.classList.toggle("on", sec.id === id);
  });
  renderNav();
  $("content").scrollTop = 0;
  loadSection(id);
}
function loadSection(id){
  if (id === "overview") return refreshStatus();
  if (!S.rendered) return;          // 参数还没渲染，等首次配置加载
  if (id === "persona") return loadPersonas(true);
  if (id === "chats") return loadChats();
  if (id === "people") return loadPeople();
  if (id === "memory") return loadMemory();
  if (id === "kb") return loadKb();
  if (id === "tools") return loadTools(true);
  if (id === "system") return loadSystem(true);
  if (id === "logs") return refreshStatus();
  return Promise.resolve();
}

/* =====================================================================
   总览
   ===================================================================== */
function renderAlert(st){
  const win = st.window || {};
  const box = $("ovAlert");
  const bad = win.state === "minimized" || (win.visible === false && win.hwnd);
  if (!bad){
    box.innerHTML = "";
    return;
  }
  box.innerHTML = '<div class="alert">' +
    '<b>微信窗口现在是最小化的，回复发不出去。</b><br>' +
    '发送前会自动把窗口还原（会抢一次前台），也可以自己点一下任务栏的微信把它开出来。' +
    '</div>';
}

function renderKpis(st){
  const mem = st.memory || {}, kb = st.kb || {}, tk = st.tokens || {};
  const pct = tk.budget ? Math.min(100, Math.round(tk.last / tk.budget * 100)) : 0;
  const toolCalls = Object.values(st.tools || {}).reduce(function(a,b){ return a + b.count; }, 0);
  const fails = (st.send_fail||0) + (st.errors||0);
  const cards = [
    { k:"消息 / 回复", v: st.messages + " / " + st.replies,
      t: st.last_message_at ? ("最后一条 " + dur(st.now - st.last_message_at) + "前") : "还没收到消息" },
    { k:"平均耗时", v: (st.avg_elapsed||0).toFixed(2), u:"s",
      t: "最近一次 " + (st.last_elapsed||0).toFixed(2) + "s" },
    { k:"上下文占用", v: tk.last||0, u:"token",
      t: tk.budget ? ("预算的 " + pct + "%（上限 " + tk.budget + "）") : "还没跑过对话" },
    { k:"有效记忆", v: (mem.active == null ? "—" : mem.active), u: mem.capacity ? ("/ " + mem.capacity) : "",
      t: mem.total != null ? ("累计 " + mem.total + " 条，作废 " + (mem.void||0)) : "机器人没在跑，读不到" },
    { k:"资料库", v: (kb.docs == null ? "—" : kb.docs), u: kb.docs == null ? "" : "篇",
      t: kb.chunks != null ? (kb.chunks + " 个分块，重建索引后可更新") : "机器人没在跑，读不到" },
    { k:"工具调用", v: toolCalls, u:"次", t: Object.keys(st.tools||{}).length + " 个工具用过" },
    { k:"失败", v: fails, t: "发送失败 " + (st.send_fail||0) + " · 处理异常 " + (st.errors||0),
      bad: fails > 0 },
    { k:"运行时长", v: st.running ? dur(st.uptime) : "—",
      t: st.running ? "已接微信，在监听" : "面板模式，机器人没跑" },
  ];
  $("kpis").innerHTML = cards.map(function(c){
    return '<div class="kpi' + (c.bad ? " bad":"") + '">' +
      '<div class="k">' + esc(c.k) + '</div>' +
      '<div class="v">' + esc(c.v) + (c.u ? '<small>' + esc(c.u) + '</small>' : '') + '</div>' +
      '<div class="t">' + esc(c.t || "") + '</div></div>';
  }).join("");
}
function renderRecent(st){
  const rows = st.recent || [];
  $("recentCount").textContent = rows.length ? ("最近 " + rows.length + " 条") : "暂无";
  if (!rows.length){
    $("recentBody").innerHTML = '<tr><td colspan="6" class="empty">还没有消息</td></tr>';
    return;
  }
  $("recentBody").innerHTML = rows.map(function(r){
    const list = r.tools || [];
    let tools = '<span class="badge">无</span>';
    if (list.length === 1) tools = '<span class="badge acc">' + esc(list[0]) + '</span>';
    else if (list.length > 1) tools = '<span class="badge acc">' + esc(list[0]) + ' +' + (list.length-1) + '</span>';
    return '<tr>' +
      '<td class="mono">' + esc(r.time) + '</td>' +
      '<td class="clip" title="' + esc(r.chat) + '">' + esc(r.chat_name) + '</td>' +
      '<td class="clip" title="' + esc(r.text) + '">' + esc(r.text) + '</td>' +
      '<td class="clip" title="' + esc(r.reply) + '">' +
        (r.ok ? "" : '<span class="badge err">未送达</span> ') + esc(r.reply) + '</td>' +
      '<td class="mono">' + (r.elapsed||0).toFixed(2) + 's</td>' +
      '<td>' + tools + '</td></tr>';
  }).join("");
}
function renderToolBars(st){
  const entries = Object.entries(st.tools || {}).sort(function(a,b){ return b[1].count - a[1].count; });
  if (!entries.length){
    $("ov-tools").innerHTML = '<div class="empty">还没调过工具</div>';
    return;
  }
  const max = entries[0][1].count || 1;
  $("ov-tools").innerHTML = entries.map(function(pair){
    const name = pair[0], t = pair[1];
    const w = Math.max(4, Math.round(t.count / max * 100));
    const tip = name + "：调用 " + t.count + " 次，失败 " + t.fail + " 次，平均 " + fmtMs(t.avg_ms);
    const fail = t.fail ? '&nbsp;<span class="f">✕' + t.fail + '</span>' : "";
    return '<div class="toolrow">' +
      '<div class="name" title="' + esc(tip) + '">' + esc(name) + '</div>' +
      '<div class="bar" title="' + esc(tip) + '"><i style="width:' + w + '%"></i></div>' +
      '<div class="num">' + t.count + ' · ' + fmtMs(t.avg_ms) + fail + '</div></div>';
  }).join("");
}

/* =====================================================================
   参数渲染与同步
   ===================================================================== */
function controlOf(f){
  const v = f.value;
  const path = esc(f.path);
  if (f.kind === "bool"){
    return '<label class="switch"><input type="checkbox" data-path="' + path + '"' +
      (v ? " checked" : "") + '><span></span></label>';
  }
  if (f.kind === "enum"){
    return '<select data-path="' + path + '">' + f.choices.map(function(c){
      return '<option value="' + esc(c) + '"' + (String(v) === c ? " selected" : "") + '>' +
        esc(c) + '</option>';
    }).join("") + '</select>';
  }
  if (f.kind === "list"){
    const text = Array.isArray(v) ? v.join(", ") : (v == null ? "" : v);
    return '<input type="text" data-path="' + path + '" value="' + esc(text) + '" placeholder="逗号分隔，留空表示不限">';
  }
  if (f.kind === "int" || f.kind === "float"){
    const step = f.step || (f.kind === "int" ? 1 : 0.01);
    return '<input type="number" data-path="' + path + '" value="' + esc(v) + '"' +
      (f.min != null ? ' min="' + f.min + '"' : '') +
      (f.max != null ? ' max="' + f.max + '"' : '') + ' step="' + step + '">';
  }
  return '<input type="text" data-path="' + path + '" value="' + esc(v) + '">';
}
function fieldHtml(f){
  return '<div class="field" data-field="' + esc(f.path) + '">' +
    '<div class="flabel">' + esc(f.label) + badgeOf(f) +
      '<span class="fpath">' + esc(f.path) + '</span></div>' +
    '<div class="frow">' + controlOf(f) +
      (f.suffix ? '<span class="suffix">' + esc(f.suffix) + '</span>' : '') +
      '<button class="green sm" data-save="' + esc(f.path) + '">保存</button>' +
      '<button class="ghost sm" data-reset="' + esc(f.path) + '"' +
        (f.overridden ? "" : " disabled") + '>默认</button></div>' +
    '<div class="fhint">' + esc(f.hint || "") +
      (f.default != null ? '<b> 默认 </b>' + esc(Array.isArray(f.default) ? (f.default.join(", ") || "（空）") : f.default) : "") +
      (f.min != null || f.max != null
        ? ' · 范围 ' + (f.min != null ? f.min : "") + ' ~ ' + (f.max != null ? f.max : "") : "") +
    '</div></div>';
}
function renderAllFields(){
  S.groups.forEach(function(group){
    const box = document.querySelector('[data-fields="' + group + '"]');
    if (!box) return;
    const list = S.fields.filter(function(f){ return f.group === group; });
    box.innerHTML = list.map(fieldHtml).join("") || '<div class="empty">这一组没有可调项</div>';
    box.querySelectorAll("input[data-path],select[data-path]").forEach(function(el){
      el.addEventListener("input", function(){ S.dirty.add(el.dataset.path); });
      el.addEventListener("change", function(){ S.dirty.add(el.dataset.path); });
    });
    box.querySelectorAll("[data-save]").forEach(function(btn){
      btn.addEventListener("click", function(){ save(btn.dataset.save, btn); });
    });
    box.querySelectorAll("[data-reset]").forEach(function(btn){
      btn.addEventListener("click", function(){ resetField(btn.dataset.reset, btn); });
    });
  });
  document.querySelectorAll("[data-reset-group]").forEach(function(btn){
    btn.addEventListener("click", function(){ resetGroup(btn.dataset.resetGroup, btn); });
  });
}
function syncFields(fields){
  S.fields = fields;
  fields.forEach(function(f){
    const el = document.querySelector('[data-path="' + f.path + '"]');
    if (el && document.activeElement !== el && !S.dirty.has(f.path)){
      if (f.kind === "bool") el.checked = !!f.value;
      else if (f.kind === "list") el.value = Array.isArray(f.value) ? f.value.join(", ") : (f.value || "");
      else el.value = f.value == null ? "" : f.value;
    }
    const box = document.querySelector('[data-field="' + f.path + '"]');
    if (!box) return;
    const head = box.querySelector(".flabel");
    head.innerHTML = esc(f.label) + badgeOf(f) + '<span class="fpath">' + esc(f.path) + '</span>';
    const reset = box.querySelector("[data-reset]");
    if (reset) reset.disabled = !f.overridden;
  });
  renderNav();
}
function readValue(path){
  const f = S.fields.find(function(x){ return x.path === path; });
  const el = document.querySelector('[data-path="' + path + '"]');
  if (!f || !el) return null;
  return f.kind === "bool" ? el.checked : el.value;
}
async function save(path, btn){
  if (btn) btn.disabled = true;
  try{
    const r = await api("/api/config", {method:"POST",
      body: JSON.stringify({path: path, value: readValue(path)})});
    S.dirty.delete(path);
    toast(path + " → " + (Array.isArray(r.value) ? (r.value.join(", ") || "（空）") : r.value), "ok");
    await refreshConfig(true);
    afterConfigChange(path);
  }catch(e){ toast(e.message, "err"); }
  finally{ if (btn) btn.disabled = false; }
}
async function resetField(path, btn){
  if (btn) btn.disabled = true;
  try{
    const r = await api("/api/config/reset", {method:"POST", body: JSON.stringify({path: path})});
    S.dirty.delete(path);
    toast(path + " 恢复默认：" + (Array.isArray(r.value) ? (r.value.join(", ") || "（空）") : r.value), "ok");
    await refreshConfig(true);
    afterConfigChange(path);
  }catch(e){ toast(e.message, "err"); }
  finally{ if (btn) btn.disabled = false; }
}
async function resetGroup(group, btn){
  const list = S.fields.filter(function(f){ return f.group === group && f.overridden; });
  if (!list.length){ toast("这一区没有被改过的参数"); return; }
  if (!confirm("把「" + group + "」这区改过的 " + list.length + " 项恢复默认？")) return;
  btn.disabled = true;
  try{
    for (const f of list){
      await api("/api/config/reset", {method:"POST", body: JSON.stringify({path: f.path})});
    }
    toast("已恢复 " + list.length + " 项默认值", "ok");
    await refreshConfig(true);
    afterConfigChange("");
  }catch(e){ toast(e.message, "err"); }
  finally{ btn.disabled = false; }
}
function afterConfigChange(path){
  if (path.indexOf("voice.") === 0) return;
  if (path.indexOf("kb.") === 0) S.loaded.kb = false;
  if (S.tab === "kb" && path.indexOf("kb.") === 0) loadKb();
}

/* =====================================================================
   角色库
   ===================================================================== */
S.personaName = "";
async function loadPersonas(keep){
  try{
    const r = await api("/api/personas");
    S.personas = r;
    if (!keep || !S.personaName || !r.items.some(function(x){ return x.name === S.personaName; })){
      S.personaName = r.default;
    }
    $("personaList").innerHTML = r.items.map(function(p){
      const badges = (p.is_default ? '<span class="badge ok">默认</span> ' : "") +
        (p.bound ? '<span class="badge acc">绑 ' + p.bound + '</span>' : "");
      return '<div class="p-item' + (p.name === S.personaName ? " on" : "") + '" data-pname="' + esc(p.name) + '">' +
        '<span>' + esc(p.name) + '</span>' +
        '<span class="meta">' + badges + ' ' + p.chars + ' 字</span></div>';
    }).join("") || '<div class="empty">还没有角色</div>';
    $("personaList").querySelectorAll("[data-pname]").forEach(function(el){
      el.addEventListener("click", function(){ pickPersona(el.dataset.pname); });
    });
    await pickPersona(S.personaName, true);
  }catch(e){ toast(e.message, "err"); }
}
async function pickPersona(name, silent){
  try{
    const r = await api("/api/persona?name=" + encodeURIComponent(name));
    S.personaName = r.name;
    $("personaName").value = r.name;
    $("personaText").value = r.text;
    $("personaMeta").textContent = "文件：" + r.path + "\\" + r.name + ".md · " +
      r.chars + " 字 / " + r.lines + " 行";
    $("personaDefaultBadge").style.display = r.is_default ? "" : "none";
    $("btnPersonaDelete").disabled = !!r.is_default;
    $("personaList").querySelectorAll("[data-pname]").forEach(function(el){
      el.classList.toggle("on", el.dataset.pname === r.name);
    });
    if (!silent) S.loaded.persona = true;
  }catch(e){ if (!silent) toast(e.message, "err"); }
}
$("btnPersonaSave").addEventListener("click", async function(){
  const btn = $("btnPersonaSave");
  btn.disabled = true;
  try{
    const name = S.personaName;   // 名字只走「改名」按钮，避免手打名字另存成新文件
    const r = await api("/api/persona", {method:"POST",
      body: JSON.stringify({name: name, text: $("personaText").value})});
    S.personaName = r.name;
    toast("「" + r.name + "」已保存，下一句就用它", "ok");
    await loadPersonas(true);
  }catch(e){ toast(e.message, "err"); }
  finally{ btn.disabled = false; }
});
$("btnPersonaNew").addEventListener("click", async function(){
  const name = prompt("新角色叫什么？（中文英文都行，别带斜杠）");
  if (!name) return;
  try{
    const r = await api("/api/personas/new", {method:"POST", body: JSON.stringify({name: name})});
    toast("建好了：「" + r.name + "」，改完点保存", "ok");
    S.personaName = r.name;
    await loadPersonas(true);
  }catch(e){ toast(e.message, "err"); }
});
$("btnPersonaRename").addEventListener("click", async function(){
  const to = prompt("改成什么名字？", S.personaName);
  if (!to || to === S.personaName) return;
  try{
    const r = await api("/api/personas/rename", {method:"POST",
      body: JSON.stringify({from: S.personaName, to: to})});
    S.personaName = r.name;
    toast("改名成功", "ok");
    await loadPersonas(true);
  }catch(e){ toast(e.message, "err"); }
});
$("btnPersonaDelete").addEventListener("click", async function(){
  if (!confirm("删掉角色「" + S.personaName + "」？绑定它的小会话会回到默认角色。")) return;
  try{
    await api("/api/personas/delete", {method:"POST",
      body: JSON.stringify({name: S.personaName})});
    toast("已删除", "ok");
    S.personaName = "";
    await loadPersonas();
  }catch(e){ toast(e.message, "err"); }
});
$("btnPersonaDefault").addEventListener("click", async function(){
  try{
    await api("/api/personas/default", {method:"POST",
      body: JSON.stringify({name: S.personaName})});
    toast("「" + S.personaName + "」现在是默认角色", "ok");
    await loadPersonas(true);
  }catch(e){ toast(e.message, "err"); }
});

/* =====================================================================
   会话监视
   ===================================================================== */
async function loadChats(){
  try{
    const r = await api("/api/chats");
    const modes = r.modes;
    S.peopleModes = r.people_modes || [];
    const defMode = { group: r.defaults.group, private: r.defaults.private };
    $("chatsHint").innerHTML = '<div class="note" style="margin:0 0 10px">' +
      '默认策略：私聊 → ' + esc(modeLabel(modes, defMode.private)) +
      ' · 群 → ' + esc(modeLabel(modes, defMode.group)) +
      ' · 未绑定会话用「' + esc(r.defaults.persona) + '」。' +
      (r.items.length ? "" : " 现在读不到会话列表——机器人没在跑，或微信没登录。") + '</div>';

    if (!r.items.length){
      $("chatsBody").innerHTML = '<tr><td colspan="6" class="empty">先让机器人跑起来，这里才会列出会话</td></tr>';
      S.loaded.chats = true;
      return;
    }
    $("chatsBody").innerHTML = r.items.map(function(c){
      const modeOptions = '<option value="">默认（' + esc(modeLabel(modes, c.group ? defMode.group : defMode.private)) + '）</option>' +
        modes.map(function(m){
          return '<option value="' + esc(m.key) + '"' + (c.mode === m.key ? " selected" : "") + '>' +
            esc(m.label) + '</option>';
        }).join("");
      const personaOptions = '<option value="">默认（' + esc(r.defaults.persona) + '）</option>' +
        r.personas.map(function(p){
          return '<option value="' + esc(p) + '"' + (c.persona === p ? " selected" : "") + '>' + esc(p) + '</option>';
        }).join("");
      const time = c.last_time ? new Date(c.last_time * 1000).toLocaleString("zh-CN", {hour12:false}) : "";
      const nPeople = (c.people || []).length;
      const peopleText = c.people_mode === "only" ? ("只理 " + nPeople + " 人")
        : c.people_mode === "ignore" ? ("不理 " + nPeople + " 人") : "全部";
      const peopleCls = c.people_mode ? "badge acc" : "badge";
      return '<tr>' +
        '<td><div>' + esc(c.name) + '</div><div class="mono" style="font-size:11px;color:var(--faint)">' +
          esc(c.id) + '</div></td>' +
        '<td><span class="badge' + (c.group ? " acc" : "") + '">' + (c.group ? "群" : "人") + '</span></td>' +
        '<td><select class="cell" data-chat-mode="' + esc(c.id) + '">' + modeOptions + '</select></td>' +
        '<td><select class="cell" data-chat-persona="' + esc(c.id) + '">' + personaOptions + '</select></td>' +
        '<td><button class="' + peopleCls + '" style="cursor:pointer" data-chat-people="' + esc(c.id) +
          '" data-chat-name="' + esc(c.name) + '">' + esc(peopleText) + '</button></td>' +
        '<td class="clip" title="' + esc(c.summary) + '">' + esc(c.summary || "—") +
          (time ? '<div class="note" style="margin:0">' + esc(time) + '</div>' : "") + '</td>' +
        '<td><button class="ghost sm" style="white-space:nowrap" data-chat-reset="' + esc(c.id) + '"' +
          (c.explicit ? "" : " disabled") + '>默认</button></td></tr>';
    }).join("");

    $("chatsBody").querySelectorAll("[data-chat-mode]").forEach(function(el){
      el.addEventListener("change", function(){ setChat(el.dataset.chatMode, {mode: el.value}); });
    });
    $("chatsBody").querySelectorAll("[data-chat-persona]").forEach(function(el){
      el.addEventListener("change", function(){ setChat(el.dataset.chatPersona, {persona: el.value}); });
    });
    $("chatsBody").querySelectorAll("[data-chat-people]").forEach(function(el){
      el.addEventListener("click", function(){
        openPeople(el.dataset.chatPeople, el.dataset.chatName);
      });
    });
    $("chatsBody").querySelectorAll("[data-chat-reset]").forEach(function(btn){
      btn.addEventListener("click", function(){ setChat(btn.dataset.chatReset, {mode:"", persona:""}); });
    });
    S.loaded.chats = true;
  }catch(e){ toast(e.message, "err"); }
}
function modeLabel(modes, key){
  for (let i = 0; i < modes.length; i++) if (modes[i].key === key) return modes[i].label;
  return key || "默认";
}
async function setChat(id, patch){
  patch.id = id;
  try{
    await api("/api/chats", {method:"POST", body: JSON.stringify(patch)});
    toast("已生效：" + id.slice(0, 12), "ok");
    await loadChats();
  }catch(e){ toast(e.message, "err"); }
}
$("btnChatsRefresh").addEventListener("click", function(){ loadChats(); });

/* =====================================================================
   用户等级
   ===================================================================== */
const PEOPLE_LEVELS = [
  { key: "owner",    label: "主人",   cls: "err"  },
  { key: "friend",   label: "熟人",   cls: "ok"   },
  { key: "stranger", label: "陌生人", cls: ""     },
  { key: "blocked",  label: "不理",   cls: "warn" },
];
function peopleLevelBadge(level){
  for (let i = 0; i < PEOPLE_LEVELS.length; i++){
    if (PEOPLE_LEVELS[i].key === level){
      return '<span class="badge ' + PEOPLE_LEVELS[i].cls + '">' + PEOPLE_LEVELS[i].label + '</span>';
    }
  }
  return '<span class="badge">' + esc(level) + '</span>';
}
async function loadPeople(){
  try{
    const r = await api("/api/people");
    const entries = Object.entries(r.items || {});
    if (!entries.length){
      // 空不是错：还没单独设过人，全部走 config 种子（owners / 默认等级）
      $("peopleBody").innerHTML = '<tr><td colspan="4" class="empty">还没单独设过任何人（走 config 种子）</td></tr>';
      return;
    }
    $("peopleBody").innerHTML = entries.map(function(pair){
      const wxid = pair[0], p = pair[1] || {};
      const level = p.level || "stranger";
      // 四个等级按钮只发 level；称呼单独走 prompt；删走 forget 接口。
      // 按钮为什么不写成一个下拉：改等级是最高频操作，一键到位比先展开下拉快。
      const buttons = PEOPLE_LEVELS.map(function(l){
        return '<button class="' + (l.key === level ? "green" : "ghost") + ' sm" ' +
          'data-people-level="' + esc(wxid) + '" data-level="' + l.key + '">' + l.label + '</button>';
      }).join(" ");
      return '<tr>' +
        '<td class="mono" title="' + esc(wxid) + '">' + esc(wxid) + '</td>' +
        '<td>' + esc(p.alias || "") + '</td>' +
        '<td>' + peopleLevelBadge(level) + '</td>' +
        '<td><div class="row" style="gap:4px">' + buttons +
          '<button class="ghost sm" data-people-alias="' + esc(wxid) + '">称呼</button>' +
          '<button class="red sm" data-people-forget="' + esc(wxid) + '">删</button></div></td></tr>';
    }).join("");
    bindPeopleRows();
  }catch(e){ toast(e.message, "err"); }
}
function bindPeopleRows(){
  $("peopleBody").querySelectorAll("[data-people-level]").forEach(function(btn){
    btn.addEventListener("click", async function(){
      btn.disabled = true;
      try{
        await api("/api/people", {method:"POST",
          body: JSON.stringify({wxid: btn.dataset.peopleLevel, level: btn.dataset.level})});
        toast("等级已改", "ok");
        await loadPeople();
      }catch(e){ toast(e.message, "err"); btn.disabled = false; }
    });
  });
  $("peopleBody").querySelectorAll("[data-people-alias]").forEach(function(btn){
    btn.addEventListener("click", async function(){
      // 空串 = 清掉称呼，null = 取消，两种都要区分开
      const alias = prompt("这个人怎么称呼？（留空清掉）", "");
      if (alias === null) return;
      btn.disabled = true;
      try{
        await api("/api/people", {method:"POST",
          body: JSON.stringify({wxid: btn.dataset.peopleAlias, alias: alias})});
        toast("称呼已保存", "ok");
        await loadPeople();
      }catch(e){ toast(e.message, "err"); btn.disabled = false; }
    });
  });
  $("peopleBody").querySelectorAll("[data-people-forget]").forEach(function(btn){
    btn.addEventListener("click", async function(){
      if (!confirm("删掉 " + btn.dataset.peopleForget + " 的等级记录？会回落到默认等级。")) return;
      btn.disabled = true;
      try{
        await api("/api/people/forget", {method:"POST",
          body: JSON.stringify({wxid: btn.dataset.peopleForget})});
        toast("已删，回落到默认", "ok");
        await loadPeople();
      }catch(e){ toast(e.message, "err"); btn.disabled = false; }
    });
  });
}
$("btnPeopleRefresh").addEventListener("click", function(){ loadPeople(); });

/* ---- 成员规则弹窗 ---- */
S.peopleCtx = null;
async function openPeople(chatId, chatName){
  try{
    const r = await api("/api/chats/people?id=" + encodeURIComponent(chatId));
    S.peopleCtx = { id: chatId, name: chatName, rule: r, picked: new Set(r.people || []) };
    $("modalTitle").textContent = "谁说的话算数";
    $("modalWho").textContent = chatName + "（" + chatId + "）· " +
      (r.group ? "群聊" : "私聊") +
      (r.items.length ? "" : " · 读不到最近消息，可以手动加名字");
    // 兜底要用「空数组也算没拿到」判断：`[]` 在 JS 里是真值，写 || 会漏
    const modes = (S.peopleModes && S.peopleModes.length) ? S.peopleModes : [
      {key:"", label:"所有人都算"}, {key:"only", label:"只理这些人"}, {key:"ignore", label:"不理这些人"}
    ];
    $("modalModes").innerHTML = modes.map(function(m){
      return '<label><input type="radio" name="pmode" value="' + esc(m.key) + '"' +
        ((r.people_mode || "") === m.key ? " checked" : "") + '> ' + esc(m.label) + '</label>';
    }).join("");
    renderPeopleList(r.items);
    $("modalAdd").value = "";
    $("modal").classList.add("on");
    $("modalAdd").focus();
  }catch(e){ toast(e.message, "err"); }
}
function renderPeopleList(items){
  const ctx = S.peopleCtx;
  const known = new Set((items || []).map(function(x){ return x.name; }));
  // 名单里可能有「最近没说话」的人：补在最后，别让人以为丢了
  const extra = [];
  ctx.picked.forEach(function(name){ if (!known.has(name)) extra.push({id:"", name: name, count: 0, last: 0}); });
  const all = (items || []).concat(extra);
  if (!all.length){
    $("modalList").innerHTML = '<div class="empty">这个会话还没有可用的人名，手动加吧</div>';
    return;
  }
  $("modalList").innerHTML = all.map(function(p){
    const on = ctx.picked.has(p.name) || ctx.picked.has(p.id);
    const when = p.last ? new Date(p.last * 1000).toLocaleDateString("zh-CN") : "";
    return '<label class="person"><input type="checkbox" data-person="' + esc(p.name) + '"' +
      (on ? " checked" : "") + '> <span>' + esc(p.name) + '</span>' +
      '<span class="meta">' + (p.count ? (p.count + " 条 · " + when) : "名单里") + '</span></label>';
  }).join("");
  $("modalList").querySelectorAll("[data-person]").forEach(function(el){
    el.addEventListener("change", function(){
      if (el.checked) ctx.picked.add(el.dataset.person);
      else ctx.picked.delete(el.dataset.person);
    });
  });
}
$("modalAdd").addEventListener("keydown", function(ev){
  if (ev.key !== "Enter") return;
  const name = $("modalAdd").value.trim();
  if (!name || !S.peopleCtx) return;
  S.peopleCtx.picked.add(name);
  $("modalAdd").value = "";
  const box = $("modalList");
  const wrap = document.createElement("label");
  wrap.className = "person";
  wrap.innerHTML = '<input type="checkbox" checked> <span>' + esc(name) + '</span>' +
    '<span class="meta">手动添加</span>';
  wrap.querySelector("input").addEventListener("change", function(el){
    if (el.target.checked) S.peopleCtx.picked.add(name);
    else S.peopleCtx.picked.delete(name);
  });
  box.appendChild(wrap);
});
$("modalCancel").addEventListener("click", function(){ $("modal").classList.remove("on"); });
$("modal").addEventListener("click", function(ev){
  if (ev.target === $("modal")) $("modal").classList.remove("on");
});
document.addEventListener("keydown", function(ev){
  if (ev.key === "Escape") $("modal").classList.remove("on");
});
$("modalSave").addEventListener("click", async function(){
  const ctx = S.peopleCtx;
  if (!ctx) return;
  const modeEl = document.querySelector('input[name="pmode"]:checked');
  const mode = modeEl ? modeEl.value : "";
  const people = Array.from(ctx.picked);
  if (mode && !people.length){
    toast("选了「" + (mode === "only" ? "只理" : "不理") + "」就得至少挑一个人", "err");
    return;
  }
  try{
    await api("/api/chats", {method:"POST",
      body: JSON.stringify({id: ctx.id, people_mode: mode, people: people})});
    $("modal").classList.remove("on");
    toast(mode ? ("成员规则已生效：" + people.length + " 人") : "已恢复：所有人都算", "ok");
    await loadChats();
  }catch(e){ toast(e.message, "err"); }
});

/* =====================================================================
   记忆
   ===================================================================== */
async function loadMemory(q, retried){
  try{
    // 归属筛选从 DOM 现读，不另存一份状态：下拉是唯一真相，
    // 多存一份 S.memOwner 就多一处「页面和请求参数不一致」的可能。
    const owner = $("memOwner").value || "";
    const r = await api("/api/memory?limit=120" +
      (q ? "&q=" + encodeURIComponent(q) : "") +
      (owner ? "&owner=" + encodeURIComponent(owner) : ""));
    const st = r.stats || {};
    $("memStats").textContent = st.active == null ? "机器人没在跑" :
      ("有效 " + st.active + " / 容量 " + st.capacity + " · 累计 " + st.total + " · 作废 " + (st.void||0));
    if (!renderOwnerOptions(r.owners || [], owner) && !retried){
      // 选中的归属已经不存在了（典型场景：最后一条未归属刚被指派走，
      // 接口就不再返回「未归属」这个选项）。下拉跳回「全部」但表里还是
      // 上一次的筛选结果，看着像筛选坏了 —— 静默重拉一次，只重试一层。
      await loadMemory(q, true);
      return;
    }
    if (q){
      if (!r.hits.length){
        $("memHits").innerHTML = '<div class="note">没有一条记忆过得了证据门槛（' +
          (S.status ? "" : "") + '宁可空着也不凑数）。</div>';
      }else{
        $("memHits").innerHTML = '<div class="tbl-wrap"><table><thead><tr>' +
          '<th>命中</th><th style="width:90px">综合分</th><th style="width:90px">相似度</th></tr></thead><tbody>' +
          r.hits.map(function(h){
            return '<tr><td>' + esc(h.text) + '</td><td class="mono">' + h.score +
              '</td><td class="mono">' + h.similarity + '</td></tr>';
          }).join("") + '</tbody></table></div>';
      }
    }else{
      $("memHits").innerHTML = "";
    }
    if (!r.items.length){
      $("memBody").innerHTML = '<tr><td colspan="8" class="empty">记忆库是空的</td></tr>';
      return;
    }
    $("memBody").innerHTML = r.items.map(function(m){
      const alias = m.alias ? '<div class="note" style="margin:2px 0 0">别名：' + esc(m.alias) + '</div>' : "";
      const note = m.note ? '<div class="note" style="margin:2px 0 0">' + esc(m.note) + '</div>' : "";
      // 未归属的行必须显眼：那批旧数据谁都查不到，混在表里看不出来就永远没人处理
      const owner = m.owner ? esc(m.owner_name)
        : '<span class="badge warn" title="没有归属 = 谁都查不到，指派一个主人它就活了">未归属</span>';
      // 「指派」只给未归属的行：有主的记忆改归属基本是手滑，真写错了走作废重记
      // 更干净。入口越少，误改越少。
      const assign = m.owner ? ""
        : '<button class="ghost sm" data-assign="' + esc(m.id) + '">指派</button>';
      return '<tr><td>' + esc(m.text) + alias + note + '</td>' +
        '<td><span class="badge">' + esc(m.type || "—") + '</span></td>' +
        '<td class="mono">' + m.importance + '</td>' +
        '<td class="mono">' + m.use_count + '</td>' +
        '<td>' + owner + '</td>' +
        '<td class="mono">' + esc(m.created) + '</td>' +
        '<td class="mono">' + esc(m.last_used) + '</td>' +
        '<td><button class="ghost sm" data-void="' + esc(m.id) + '">作废</button>' +
        assign + '</td></tr>';
    }).join("");
    $("memBody").querySelectorAll("[data-void]").forEach(function(btn){
      btn.addEventListener("click", async function(){
        if (!confirm("作废这条记忆？作废后不再进检索（记录还在日志里）。")) return;
        btn.disabled = true;
        try{
          await api("/api/memory/void", {method:"POST", body: JSON.stringify({id: btn.dataset.void})});
          toast("已作废", "ok");
          await loadMemory($("memQuery").value.trim());
        }catch(e){ toast(e.message, "err"); btn.disabled = false; }
      });
    });
    $("memBody").querySelectorAll("[data-assign]").forEach(function(btn){
      btn.addEventListener("click", async function(){
        // 故意用 prompt 而不是做一套选择器：指派是低频的补救动作，
        // 为它把 people 全拉一遍、再造个弹窗不值得，手填 wxid 一步到位。
        const who = prompt("指派给谁？填 wxid（例如 wxid_abc123）。\n" +
          "群公共记忆可以填 g:群id，留空则不改。");
        if (!who || !who.trim()) return;
        btn.disabled = true;
        try{
          // 只填 wxid 也认：服务端会补 u: 前缀（谁记得住那个前缀）
          const res = await api("/api/memory/assign",
            {method:"POST", body: JSON.stringify({id: btn.dataset.assign, owner: who.trim()})});
          toast("已指派给 " + (res.owner_name || who.trim()), "ok");
          await loadMemory($("memQuery").value.trim());
        }catch(e){ toast(e.message, "err"); btn.disabled = false; }
      });
    });
    S.loaded.memory = true;
  }catch(e){ toast(e.message, "err"); }
}
/* 归属下拉：全部 / 未归属（N）/ 每个人。
   选项和排序都用接口给的 owners（接口已按条数降序、未归属排最后）——
   同一套规则只放一处，页面和接口各排一遍迟早会不一致。
   返回 false 表示「当前选中的值已经不在选项里了」，调用方据此决定要不要重拉。 */
function renderOwnerOptions(items, current){
  const sel = $("memOwner");
  const html = ['<option value="">全部归属</option>'];
  items.forEach(function(o){
    html.push('<option value="' + esc(o.owner) + '">' +
      esc(o.name) + '（' + o.count + '）</option>');
  });
  sel.innerHTML = html.join("");
  const found = items.some(function(o){ return o.owner === current; });
  sel.value = (current && found) ? current : "";
  return !current || found;
}
$("btnMemRefresh").addEventListener("click", function(){ loadMemory($("memQuery").value.trim()); });
$("btnMemSearch").addEventListener("click", function(){ loadMemory($("memQuery").value.trim()); });
$("memOwner").addEventListener("change", function(){ loadMemory($("memQuery").value.trim()); });
$("memQuery").addEventListener("keydown", function(ev){
  if (ev.key === "Enter") loadMemory($("memQuery").value.trim());
});

/* =====================================================================
   资料库
   ===================================================================== */
async function loadKb(){
  try{
    const r = await api("/api/kb");
    $("kbStats").textContent = r.stats.docs + " 篇 / " + r.stats.chunks + " 块 / " +
      r.stats.chars + " 字";
    if (!r.docs.length){
      $("kbBody").innerHTML = '<tr><td colspan="5" class="empty">还没有索引任何文档。' +
        '把 md / txt / pdf / docx 放进 knowledge/，再点「重建索引」。</td></tr>';
      return;
    }
    $("kbBody").innerHTML = r.docs.map(function(d){
      const when = new Date((d.mtime || 0) * 1000).toLocaleString("zh-CN", {hour12:false});
      return '<tr><td>' + esc(d.title || "（无标题）") + '</td>' +
        '<td class="clip mono" title="' + esc(d.path) + '">' + esc(d.path) + '</td>' +
        '<td class="mono">' + d.chars + '</td><td class="mono">' + d.chunks + '</td>' +
        '<td class="mono">' + esc(when) + '</td></tr>';
    }).join("");
    S.loaded.kb = true;
  }catch(e){ toast(e.message, "err"); }
}
async function reindex(btn){
  if (btn) btn.disabled = true;
  try{
    const r = await api("/api/action/reindex", {method:"POST", body:"{}"});
    toast("索引好了：更新 " + r.files + " 篇，新增 " + r.chunks + " 块，现有 " + r.docs + " 篇", "ok");
    await loadKb();
  }catch(e){ toast(e.message, "err"); }
  finally{ if (btn) btn.disabled = false; }
}
$("btnKbReindex").addEventListener("click", function(){ reindex($("btnKbReindex")); });
$("btnReindexTop").addEventListener("click", function(){ reindex($("btnReindexTop")); });

/* =====================================================================
   工具清单
   ===================================================================== */
async function loadTools(force){
  if (S.loaded.tools && !force) return;
  try{
    const r = await api("/api/tools");
    const running = S.status && S.status.mode === "run";
    const hint = running ? "" :
      '<div class="note">面板是 standalone 模式（机器人没跑），' +
      'remember / kb_search / chat_history 这类要真实数据的工具这会儿看不到。' +
      '启动机器人后再来看就是完整的。</div>';
    if (!r.items.length){
      $("toolsBody").innerHTML = '<tr><td colspan="4" class="empty">一个工具都没挂上</td></tr>';
      return;
    }
    $("toolsBody").innerHTML = r.items.map(function(t){
      return '<tr><td class="mono">' + esc(t.name) + '</td>' +
        '<td>' + esc(t.description) + '</td>' +
        '<td class="mono">' + esc((t.params || []).join(", ") || "—") + '</td>' +
        '<td class="mono">' + t.count + (t.fail ? ' <span class="badge err">失败 ' + t.fail + '</span>' : '') +
          '<div class="note" style="margin:0">' + fmtMs(t.avg_ms) + '</div></td></tr>';
    }).join("") + '<tr><td colspan="4" style="border:none;padding:0">' + hint + '</td></tr>';
    S.loaded.tools = true;
  }catch(e){ toast(e.message, "err"); }
}
$("btnToolsRefresh").addEventListener("click", function(){ loadTools(true); });

/* =====================================================================
   语音试听
   ===================================================================== */
$("btnVoiceTest").addEventListener("click", async function(){
  const btn = $("btnVoiceTest");
  const text = $("voiceText").value.trim();
  if (!text){ toast("先写一句要念的话", "err"); return; }
  btn.disabled = true;
  $("voicePlayer").innerHTML = '<div class="note">合成中…（要是没联网会失败）</div>';
  try{
    const r = await api("/api/voice/test", {method:"POST", body: JSON.stringify({text: text})});
    $("voicePlayer").innerHTML = '<audio controls autoplay src="' + esc(r.url) + '"></audio>' +
      '<span class="note" style="margin-left:10px">' + esc(r.file) + '</span>';
    toast("合成好了，直接播", "ok");
  }catch(e){
    $("voicePlayer").innerHTML = "";
    toast(e.message, "err");
  }finally{ btn.disabled = false; }
});

/* =====================================================================
   日志
   ===================================================================== */
function renderLogs(st){
  const logs = st.logs || [];
  const q = $("logQuery").value.trim().toLowerCase();
  const lv = $("logLevel").value;
  const order = {DEBUG:0, INFO:1, WARNING:2, ERROR:3, CRITICAL:4};
  const shown = logs.filter(function(l){
    if (lv && (order[l.level] || 0) < order[lv]) return false;
    if (q && (l.msg + " " + l.name + " " + l.level).toLowerCase().indexOf(q) < 0) return false;
    return true;
  });
  $("logCount").textContent = shown.length + " / " + logs.length + " 条";
  if (!shown.length){
    $("logs").innerHTML = '<div class="empty">' + (logs.length ? "过滤后没有匹配的日志" : "还没有日志") + '</div>';
    return;
  }
  const box = $("logs");
  box.innerHTML = shown.map(function(l){
    return '<div class="log ' + esc(l.level) + '">' +
      '<span>' + esc(l.time) + '</span>' +
      '<span class="lv">' + esc(l.level) + '</span>' +
      '<span class="src">' + esc(l.name) + '</span>' +
      '<span>' + esc(l.msg) + '</span></div>';
  }).join("");
  if ($("logAuto").checked) box.scrollTop = box.scrollHeight;
}
$("logQuery").addEventListener("input", function(){ if (S.status) renderLogs(S.status); });
$("logLevel").addEventListener("change", function(){ if (S.status) renderLogs(S.status); });

/* =====================================================================
   系统
   ===================================================================== */
async function loadSystem(force){
  if (S.loaded.system && !force) return;
  try{
    const r = await api("/api/system");
    const rows = [
      ["运行模式", r.mode === "run" ? "run（接了微信，面板同进程）" : "web（只开面板，机器人没跑）"],
      ["面板地址", r.panel],
      ["项目根", r.root],
      ["数据目录", r.data_dir],
      ["配置文件", r.config],
      ["覆盖文件", r.override + (Object.keys(r.overrides || {}).length ? "" : "（尚未创建）")],
      ["人设文件", r.persona],
      ["日志文件", r.log],
      ["资料库", r.kb],
      ["Python", r.python],
      ["系统", r.platform],
      ["进程号", r.pid],
    ];
    Object.keys(r.deps || {}).forEach(function(k){ rows.push(["依赖 " + k, r.deps[k]]); });
    $("sysKv").innerHTML = rows.map(function(pair){
      return '<div class="k">' + esc(pair[0]) + '</div><div class="v">' + esc(pair[1]) + '</div>';
    }).join("");

    const overrides = Object.entries(r.overrides || {});
    $("ovCount").textContent = overrides.length ? (overrides.length + " 项") : "无";
    $("overrideList").innerHTML = overrides.length
      ? '<div class="tbl-wrap"><table><thead><tr><th style="width:240px">参数</th><th>当前值</th></tr></thead><tbody>' +
        overrides.map(function(pair){
          const v = Array.isArray(pair[1]) ? (pair[1].join(", ") || "（空）") : String(pair[1]);
          return '<tr><td class="mono">' + esc(pair[0]) + '</td><td>' + esc(v) + '</td></tr>';
        }).join("") + '</tbody></table></div>'
      : "没有改过任何参数，全部用的 config.yaml 默认值。";
    S.loaded.system = true;
  }catch(e){ toast(e.message, "err"); }
}
$("btnSysRefresh").addEventListener("click", function(){ loadSystem(true); });

/* =====================================================================
   维护动作
   ===================================================================== */
$("btnSnapshot").addEventListener("click", async function(){
  try{
    const r = await api("/api/action/snapshot", {method:"POST", body:"{}"});
    $("maintNote").textContent = "已落盘，记忆库现有 " + (r.memory ? r.memory.active : "?") + " 条有效";
    toast("记忆已落盘", "ok");
  }catch(e){ toast(e.message, "err"); }
});
$("btnClearRecent").addEventListener("click", async function(){
  try{ await api("/api/action/clear-recent", {method:"POST", body:"{}"});
    toast("面板记录清了（磁盘日志没动）", "ok"); refreshStatus(); }
  catch(e){ toast(e.message, "err"); }
});
async function resetAll(){
  if (!confirm("把所有参数恢复成 config.yaml 里的默认值？")) return;
  try{
    await api("/api/action/reset-all", {method:"POST", body:"{}"});
    toast("全部恢复默认", "ok");
    await refreshConfig(true);
    loadSystem(true);
  }catch(e){ toast(e.message, "err"); }
}
$("btnResetAllTop").addEventListener("click", resetAll);

/* =====================================================================
   刷新循环
   ===================================================================== */
async function refreshStatus(){
  const st = await api("/api/status");
  S.status = st;
  const pill = $("runpill");
  pill.className = "pill " + (st.running ? "on" : "off");
  pill.innerHTML = '<i class="dot"></i><span>' + (st.running ? "运行中" : "未运行") + '</span>';
  $("acct").textContent = st.account || "未接微信";
  $("uptime").textContent = st.running ? dur(st.uptime) : "—";
  $("addr").textContent = st.panel || "—";
  $("tick").textContent = new Date().toLocaleTimeString("zh-CN", {hour12:false});
  $("ov-sub").textContent = st.mode === "run" ? "与机器人同进程" : "面板模式";
  renderAlert(st);
  renderKpis(st);
  renderRecent(st);
  renderToolBars(st);
  if (S.tab === "logs" && !$("logPause").checked) renderLogs(st);
}
async function refreshConfig(force){
  const data = await api("/api/config");
  S.groups = data.groups;
  if (!S.rendered || force){
    S.fields = data.fields;
    renderAllFields();
    S.rendered = true;
    renderNav();
  }else{
    syncFields(data.fields);
  }
}
async function tick(){
  try{ await refreshStatus(); }catch(e){ /* 面板自己也得活着 */ }
  try{ await refreshConfig(false); }catch(e){}
}

/* 启动 */
renderNav();
refreshConfig(true)
  .then(function(){
    // 地址栏带 #memory 这种就直接开那一区，刷新页面也不会跳回总览
    const want = location.hash.slice(1);
    const known = SECTIONS.some(function(sec){ return sec.id === want; });
    go(known ? want : (savedTab() || "overview"));
    return refreshStatus();
  })
  .catch(function(e){ toast(e.message, "err"); });
setInterval(tick, 2000);
</script>
</body>
</html>
"""
