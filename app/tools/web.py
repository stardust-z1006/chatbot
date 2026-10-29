# -*- coding: utf-8 -*-
"""联网：搜索 + 读网页。

和老项目的关键区别：老项目为了联网，额外配了一个「自带联网能力的模型」
（ONLINE_BASE_URL），本地搜索还要再调一次模型把资料总结成一段话——等于
一次提问打两次请求。这里搜索只负责把资料原样交回给主模型，由主模型在
同一次对话里直接消化，省一次调用，也不丢细节。
"""
from __future__ import annotations

import html
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple

import requests

from .registry import Tool

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
MAX_HTML_BYTES = 800_000
_CACHE: Dict[str, Tuple[float, List[Dict[str, str]]]] = {}
_CACHE_TTL = 300.0
# 进程要连续跑好几天，缓存不设上限就是慢性内存泄漏：
# 用户问过的每一条搜索词都会留下一个永不删除的 key。
_CACHE_CAP = 128


def _cache_put(key: str, items: List[Dict[str, str]]) -> None:
    _CACHE[key] = (time.time(), items)
    if len(_CACHE) <= _CACHE_CAP:
        return
    # 先清过期的；还不够就按时间丢掉最旧的一半
    now = time.time()
    for old_key in [k for k, (stamp, _) in _CACHE.items() if now - stamp >= _CACHE_TTL]:
        _CACHE.pop(old_key, None)
    if len(_CACHE) > _CACHE_CAP:
        oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: len(_CACHE) // 2]
        for old_key, _ in oldest:
            _CACHE.pop(old_key, None)


def _headers() -> Dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


# ------------------------------------------------------------------ 搜索
def _bing_rss(query: str, count: int, timeout: float) -> List[Dict[str, str]]:
    url = "https://www.bing.com/search?format=rss&count=%d&q=%s" % (
        count,
        urllib.parse.quote_plus(query),
    )
    resp = requests.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    out: List[Dict[str, str]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        out.append(
            {
                "title": html.unescape(title),
                "url": link,
                "snippet": html.unescape((item.findtext("description") or "").strip()),
            }
        )
        if len(out) >= count:
            break
    return out


def _bing_html(query: str, count: int, timeout: float) -> List[Dict[str, str]]:
    from bs4 import BeautifulSoup

    url = "https://www.bing.com/search?setlang=zh-CN&mkt=zh-CN&q=%s" % (
        urllib.parse.quote_plus(query)
    )
    resp = requests.get(url, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text[:MAX_HTML_BYTES], "lxml")
    out: List[Dict[str, str]] = []
    for li in soup.select("li.b_algo"):
        link = li.find("a", href=True)
        if not link:
            continue
        caption = li.select_one(".b_caption p") or li.find("p")
        out.append(
            {
                "title": link.get_text(" ", strip=True),
                "url": link["href"],
                "snippet": caption.get_text(" ", strip=True) if caption else "",
            }
        )
        if len(out) >= count:
            break
    return out


def search(query: str, count: int = 6, timeout: float = 12.0) -> List[Dict[str, str]]:
    key = "%s|%d" % (query, count)
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    for label, fn in (("rss", _bing_rss), ("html", _bing_html)):
        try:
            items = fn(query, count, timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bing %s 失败（%s）：%s", label, query[:40], exc)
            continue
        if items:
            _cache_put(key, items)
            return items
    return []


# ------------------------------------------------------------------ 抓正文
def fetch_text(url: str, max_chars: int = 3000, timeout: float = 12.0) -> str:
    from bs4 import BeautifulSoup

    if not url.startswith(("http://", "https://")):
        return ""
    try:
        resp = requests.get(url, headers=_headers(), timeout=timeout, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        logger.info("抓取失败 %s：%s", url[:80], exc)
        return ""
    if resp.status_code >= 400:
        return ""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype and "html" not in ctype and "text" not in ctype:
        return ""
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"

    soup = BeautifulSoup(resp.text[:MAX_HTML_BYTES], "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "header",
                     "footer", "nav", "form", "aside"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    return text[:max_chars]


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ 工具实现
def make_tools(cfg) -> List[Tool]:
    max_results = int(cfg.get("search.max_results", 6))
    fetch_pages = int(cfg.get("search.fetch_pages", 2))
    page_chars = int(cfg.get("search.page_chars", 3000))
    timeout = float(cfg.get("search.timeout", 12))

    def web_search(query: str, pages: int = 0) -> str:
        items = search(query, count=max_results, timeout=timeout)
        if not items:
            return "（没搜到结果，可能是网络问题或关键词太生僻）"

        # 同一站点最多抓一篇正文：两篇都是导航页的情况很常见
        want_pages = fetch_pages if pages <= 0 else min(pages, 3)
        blocks: List[str] = []
        seen = set()
        page_count = 0
        for item in items:
            blocks.append(
                "[%d] %s\n%s\n来源：%s"
                % (len(blocks) + 1, item["title"], item["snippet"] or "（无摘要）", item["url"])
            )
            if page_count >= want_pages:
                continue
            host = _domain(item["url"])
            if host in seen:
                continue
            body = fetch_text(item["url"], max_chars=page_chars, timeout=timeout)
            if len(body) < 120:
                continue
            seen.add(host)
            page_count += 1
            blocks.append("正文《%s》%s\n%s" % (item["title"], item["url"], body))

        return "\n\n".join(blocks)[:12000]

    def read_url(url: str) -> str:
        text = fetch_text(url, max_chars=page_chars * 2, timeout=timeout)
        return text or "（这个链接打不开或者没有正文）"

    return [
        Tool(
            name="web_search",
            description=(
                "联网搜索。凡是需要最新/实时信息（天气、新闻、股价、比分、价格、"
                "最近发生的事、你不确定的事实）都先用它，不要凭记忆猜。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，尽量具体"},
                },
                "required": ["query"],
            },
            func=web_search,
        ),
        Tool(
            name="read_url",
            description="打开一个具体网址，读它的正文。当对方发来链接、或者说「你看看这个」时用。",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "完整网址"}},
                "required": ["url"],
            },
            func=read_url,
        ),
    ]
