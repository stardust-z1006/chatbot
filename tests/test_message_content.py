# -*- coding: utf-8 -*-
"""回归：原始消息 → 能进模型的文本（XML 折叠 + 非文本占位）。

    .venv\\Scripts\\python.exe tests\\test_message_content.py

验的是两件以前会直接毁掉对话质量的事：

1. `local_type=49` 的正文是一整段 appmsg XML，原样进上下文会让模型照着 XML
   编造「你发的链接讲的是××」，入库判据还会把它当陈述记下来；
2. 图片/语音/视频/表情没有文字可读，上层以前直接丢弃——对方发图问「这怎么办」，
   机器人一声不吭。

纯字符串处理，不调模型、不接微信，毫秒级。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.message_content import (
    MAX_TEXT,
    fold_appmsg,
    is_placeholder,
    render,
)
from app.message_tagging import worth_remembering
from _support import FAILS, check, line, total

LINK_XML = (
    '<?xml version="1.0"?><msg><appmsg appid="" sdkver="0">'
    "<title>国务院关于印发新一代人工智能发展规划的通知</title>"
    "<des>到2030年人工智能理论、技术与应用总体达到世界领先水平</des>"
    "<type>5</type>"
    "<url>https://www.gov.cn/zhengce/content/2017-07/20/content_5211996.htm</url>"
    "<appattach><totallen>0</totallen><fileext></fileext></appattach>"
    "</appmsg></msg>"
)

FILE_XML = (
    "<msg><appmsg><title>简历.pdf</title><type>6</type>"
    "<appattach><totallen>245760</totallen><fileext>pdf</fileext>"
    "<filename>简历.pdf</filename></appattach></appmsg></msg>"
)

QUOTE_XML = (
    "<msg><appmsg><title>那我六点过去</title><type>57</type>"
    "<refermsg><type>1</type><displayname>老张</displayname>"
    "<content>晚上一起吃个饭？</content></refermsg></appmsg></msg>"
)


def main() -> int:
    line("1. type=49 的 XML 折叠成人话")
    link = fold_appmsg(LINK_XML)
    check("链接折出标题", link.startswith("[链接]") and "人工智能发展规划" in link, link)
    check("折叠结果不含任何标签", "<" not in link and "appmsg" not in link, link)
    check("折叠结果不含 URL 原文", "gov.cn/zhengce" not in link, link)

    f = fold_appmsg(FILE_XML)
    check("文件折出名字和大小", f == "[文件] 简历.pdf（240KB）", f)

    # 光测 fold_appmsg 不够：真正决定「模型看到什么」的是 render 有没有调它。
    # 这两条断的是接线，不是函数本身。
    wired, alone = render("link", LINK_XML)
    check("render 对 type=49 真的做了折叠", "<" not in wired and "人工智能" in wired, wired)
    check("折出内容的链接不算占位", not alone, wired)

    q = fold_appmsg(QUOTE_XML)
    check("引用带上被引用的原文", "晚上一起吃个饭" in q and "老张" in q, q)
    check("引用保留自己说的话", "那我六点过去" in q, q)

    line("2. 折叠要能扛住脏数据")
    b64 = (
        "<msg><appmsg><title>看图</title><type>5</type>"
        "<url>https://x.com/a</url><thumburl>" + "A" * 200 + "</thumburl></appmsg></msg>"
    )
    got = fold_appmsg(b64)
    check("长 base64 串被清掉", got == "[链接] 看图", got)

    broken = "<msg><appmsg><title>截断了"
    check("截断的 XML 不抛异常", isinstance(fold_appmsg(broken), str))
    check("截断的 XML 退成空标签，不瞎猜内容",
          fold_appmsg(broken) == "[卡片]", repr(fold_appmsg(broken)))

    long_title = "<msg><appmsg><title>" + "长" * 300 + "</title><type>5</type></appmsg></msg>"
    got = fold_appmsg(long_title)
    check(f"超长标题截到 {MAX_TEXT} 字以内", len(got) <= MAX_TEXT + 8, f"{len(got)} 字")

    check("纯文本不当 XML 折", fold_appmsg("在吗") == "", repr(fold_appmsg("在吗")))

    line("3. 非文本消息不能静默丢弃")
    for kind, mark in [("image", "[图片"), ("voice", "[语音"),
                       ("video", "[视频"), ("emoji", "[表情"),
                       ("location", "[位置")]:
        text, alone = render(kind, "")
        check(f"{kind} 给出占位而不是空", text.startswith(mark) and alone, text)

    text, alone = render("image", "")
    check("占位写明「看不到内容」", "看不到" in text, text)

    text, alone = render("voice", "今天下午三点开会")
    check("语音有转写就直接当文本用", text == "今天下午三点开会" and not alone, text)

    text, alone = render("voice", "<msg><voicemsg length='3000'/></msg>")
    check("语音没转写时是占位", text == "[语音·听不到声音]" and alone, text)

    text, alone = render("location", "<msg><location x='1' y='2'/></msg>")
    check("位置不会被折成 [卡片]", text == "[位置]" and alone, text)

    text, alone = render("redpacket", "<msg><wcpayinfo/></msg>")
    check("红包有独立标记", text == "[红包]" and alone, text)

    text, alone = render("custom", "真的是一句话")
    check("不认识的类型有正文就当文本", text == "真的是一句话" and not alone, text)

    text, alone = render("text", "在吗")
    check("文本原样透传", text == "在吗" and not alone, text)

    line("4. 系统消息不该进上下文")
    check("系统消息返回空", render("system", "<sysmsg type='pat'>") == ("", False))
    check("通话记录返回空", render("call", "") == ("", False))

    line("5. 占位不能进记忆库")
    text, _ = render("image", "")
    check("图片占位不值得记", not worth_remembering(text), text)
    check("语音占位不值得记", not worth_remembering(render("voice", "")[0]))
    check("合并后的纯占位也不记",
          not worth_remembering("[图片·看不到内容]\n[表情]"),
          "[图片·看不到内容]\\n[表情]")
    check("真话照样记", worth_remembering("我每天早上都喝美式"), "我每天早上都喝美式")
    check("占位混着真话时按真话算",
          worth_remembering("[图片·看不到内容]\n我每天早上都喝美式"))
    check("用户自己发的方括号短句不算占位", not is_placeholder("[好]"), "[好]")

    line("6. 折叠不出正文的卡片按占位处理")
    text, alone = render("link", "<msg><appmsg><title>截断了")
    check("截断链接退成 [卡片] 且标记占位", text == "[卡片]" and alone, text)
    text, alone = render("unknown", "<msg><appmsg><type>999</type><title>某卡片</title></appmsg></msg>")
    check("没见过的类型仍能取到标题", text == "[卡片] 某卡片" and not alone, text)

    print()
    print("-" * 60)
    print(f"  断言 {total()} 项，失败 {len(FAILS)} 项")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
