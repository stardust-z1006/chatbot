# -*- coding: utf-8 -*-
"""天气。

单独做一个工具，是因为「今天冷不冷」是微信里最高频的提问，而通用搜索在这
类问题上很烂：搜出来的多是天气网站的导航页，正文还是 JS 渲染的，抓下来只有
一堆城市列表。这里直接走中国天气网的三个接口（都是国内节点，快）：

    toy1.weather.com.cn/search   城市名 → 城市编码
    d1.weather.com.cn/sk_2d      实时天气（JSON）
    www.weather.com.cn/weather   7 天预报（HTML）
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests

from .registry import Tool

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://www.weather.com.cn/",
}


def _city_code(city: str, timeout: float) -> Optional[str]:
    url = "http://toy1.weather.com.cn/search?cityname=%s" % requests.utils.quote(city)
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.encoding = "utf-8"
    text = resp.text.strip()
    # 返回的是被包了一层的 JSON 数组字符串
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        return None
    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    for item in items:
        ref = str(item.get("ref") or "")
        code = ref.split("~")[0]
        # 只要正儿八经的城市编码（9 位数字），景区/街道的编码用不了
        if re.fullmatch(r"1\d{8}", code):
            return code
    return None


def _realtime(code: str, timeout: float) -> str:
    url = "https://d1.weather.com.cn/sk_2d/%s.html" % code
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.encoding = "utf-8"
    match = re.search(r"\{.*\}", resp.text, re.S)
    if not match:
        return ""
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ""
    parts = ["%s 现在 %s℃，%s%s，湿度 %s" % (
        data.get("cityname", ""),
        data.get("temp", "?"),
        data.get("WD", ""),
        data.get("WS", ""),
        data.get("SD", "?"),
    )]
    return "，".join(parts) if parts else ""


def _forecast(code: str, timeout: float) -> str:
    from bs4 import BeautifulSoup

    url = "http://www.weather.com.cn/weather/%s.shtml" % code
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")
    lines = []
    for li in soup.select("ul.t li")[:4]:
        text = li.get_text(" ", strip=True)
        if text:
            lines.append(text)
    return "\n".join(lines)


def make_tool(cfg) -> Tool:
    timeout = float(cfg.get("search.timeout", 10))

    def weather(city: str) -> str:
        city = (city or "").strip()
        if not city:
            return "（要给个城市名）"
        try:
            code = _city_code(city, timeout)
        except Exception as exc:
            logger.info("查城市编码失败：%s", exc)
            code = None
        if not code:
            return "（没找到这个城市，换个说法试试，比如「郑州」而不是「郑州市金水区」）"

        blocks = []
        try:
            now_text = _realtime(code, timeout)
            if now_text:
                blocks.append(now_text)
        except Exception as exc:
            logger.info("实时天气失败：%s", exc)
        try:
            forecast = _forecast(code, timeout)
            if forecast:
                blocks.append("未来几天：\n" + forecast)
        except Exception as exc:
            logger.info("天气预报失败：%s", exc)

        return "\n".join(blocks) if blocks else "（天气数据没取到）"

    return Tool(
        name="weather",
        description=(
            "查某个城市的实时天气和未来几天预报。问「今天冷吗」「要不要带伞」"
            "「明天多少度」这类问题用这个，比 web_search 准。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，如 郑州 / 北京 / 上海"},
            },
            "required": ["city"],
        },
        func=weather,
    )
