# -*- coding: utf-8 -*-
"""本地知识库：把文件夹里的资料切块建索引，让模型自己去查。

和老项目最大的不同是「谁来决定查不查」——老项目靠另一个模型/关键词先判定
要不要联网，再拼一段固定提示；这里直接把 kb_search 当成工具给模型，它自己
决定查什么词、要不要查第二次。
"""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, List, Tuple

from ..kb_store import KbStore
from .registry import Tool

logger = logging.getLogger(__name__)

CHUNK_CHARS = 600
CHUNK_OVERLAP = 80
SUPPORTED = {".md", ".txt", ".markdown", ".docx", ".pdf", ".py", ".json", ".csv"}


# ------------------------------------------------------------------ 读文件
def _read_text_file(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return ""


def _read_docx(path: Path) -> str:
    """docx 本质是 zip + xml，直接抽 w:t 文本，省掉 python-docx 依赖。"""
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return re.sub(r"\n{3,}", "\n\n", text)


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("没装 pypdf，跳过 PDF：%s", path.name)
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF 解析失败 %s：%s", path.name, exc)
        return ""
    pages = []
    for page in reader.pages[:200]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(pages)


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".pdf":
        return _read_pdf(path)
    return _read_text_file(path)


# ------------------------------------------------------------------ 切块
def split_chunks(text: str, size: int = CHUNK_CHARS,
                 overlap: int = CHUNK_OVERLAP) -> List[str]:
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: List[str] = []
    buf = ""
    for para in paragraphs:
        if len(buf) + len(para) + 1 <= size:
            buf = f"{buf}\n{para}".strip()
            continue
        if buf:
            chunks.append(buf)
        if len(para) <= size:
            buf = para
        else:
            step = max(1, size - overlap)
            for i in range(0, len(para), step):
                piece = para[i:i + size]
                if piece.strip():
                    chunks.append(piece.strip())
                if i + size >= len(para):
                    break
            buf = ""
    if buf:
        chunks.append(buf)
    return [c for c in chunks if len(c) >= 20]


# ------------------------------------------------------------------ 建索引
def _as_list(value: Any) -> List[str]:
    """把配置项统一成字符串列表。

    yaml 里写 `dirs: knowledge`（没加方括号）是人之常情，直接 for 会按**字符**遍历，
    于是去找 d / o / c / s 四个目录，一个都不存在——索引永远是空的，还不报错。
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = [str(x) for x in value]
    else:
        items = [str(value)]
    return [x.strip() for x in items if x and x.strip()]


def iter_documents(dirs: Iterable[str], exts: Iterable[str]) -> Iterable[Path]:
    allowed = {e if e.startswith(".") else "." + e for e in _as_list(exts)} or SUPPORTED
    for raw in _as_list(dirs):
        base = Path(raw)
        if not base.exists():
            continue
        if base.is_file():
            if base.suffix.lower() in allowed:
                yield base
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in allowed:
                yield path


def build_index(store: KbStore, cfg, force: bool = False) -> Tuple[int, int]:
    """扫描配置里的目录，增量更新索引。返回（处理文件数, 新增块数）。

    增量体现在两处：内容没改的跳过；源文件已经删掉的，索引里的也一起清掉
    —— 否则对方问到的东西明明删了，机器人还在照旧文档念。
    """
    dirs = _as_list(cfg.get("kb.dirs", ["knowledge"])) or ["knowledge"]
    exts = _as_list(cfg.get("kb.exts", [])) or list(SUPPORTED)
    dirs = [str(cfg.rel(d) if not Path(d).is_absolute() else Path(d)) for d in dirs]

    files = list(iter_documents(dirs, exts))
    processed = 0
    total_chunks = 0
    seen = set()

    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        key = str(path.resolve())
        seen.add(key)
        if not force and store.doc_mtime(key) == mtime:
            continue

        text = read_document(path)
        if not text.strip():
            logger.info("空文件，跳过：%s", path.name)
            continue

        chunks = split_chunks(text)
        doc_id = store.upsert_doc(key, path.stem, mtime, len(text), len(chunks))
        store.replace_chunks(doc_id, chunks)
        processed += 1
        total_chunks += len(chunks)
        logger.info("索引 %s → %d 块", path.name, len(chunks))

    _prune(store, dirs, seen)
    return processed, total_chunks


def _prune(store: KbStore, dirs: List[str], seen: set) -> None:
    """清掉「源文件已经没了」的旧索引。

    判据只有一条：**索引里那条指向的文件不存在了**。文件还在的一律不动——
    哪怕它这次没被扫到（后缀不在 `kb.exts` 里、目录刚改过），留着也比清掉好：
    清错了就是「有资料却查不到」，那比多留一条更难受。

    这也是唯一安全的判据。曾经想按「不在当前 dirs 下就清」来清，但那等于
    把「改错一个字母」变成「索引全空」——目录配置写错时所有条目都「不在下面」。
    """
    roots = [Path(d) for d in dirs if Path(d).exists()]
    if not roots:
        # 目录一个都不在：多半是还没建或者路径写错了，这时候清空最糟。
        return
    removed = 0
    for old in store.all_paths():
        if old in seen:
            continue
        path = Path(old)
        if path.exists():
            continue
        store.delete_doc(old)
        removed += 1
        logger.info("源文件已删除，清掉索引：%s", path.name)
    if removed:
        logger.info("本次清理过期索引 %d 篇", removed)


# ------------------------------------------------------------------ 工具
def make_tool(store: KbStore, cfg) -> Tool:
    top_k = int(cfg.get("kb.top_k", 4))

    def kb_search(query: str, count: int = 0) -> str:
        limit = count if count > 0 else top_k
        hits = store.search(query, limit=min(limit, 8))
        if not hits:
            stats = store.stats()
            if stats["docs"] == 0:
                return "（资料库里还没索引任何文档，让对方先往 knowledge/ 里放资料并重建索引）"
            return "（资料库里没有相关内容）"

        blocks = []
        for hit in hits:
            blocks.append("《%s》\n%s" % (hit["title"], hit["text"]))
        return "\n\n---\n\n".join(blocks)[:8000]

    return Tool(
        name="kb_search",
        description=(
            "查主人自己的资料库（本地文档、笔记、合同、产品资料等）。"
            "当对方问的是「我的/我们的/资料里」写过的内容，或者涉及具体细节、"
            "条款、数据时，先查资料库再回答，不要凭空编。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或短句"},
                "count": {"type": "integer", "description": "取几段，默认 4，最多 8"},
            },
            "required": ["query"],
        },
        func=kb_search,
    )
