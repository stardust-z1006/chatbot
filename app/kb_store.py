# -*- coding: utf-8 -*-
"""资料库索引（独立于记忆）。

单独一份 SQLite，和记忆库不掺一起，原因是两者的读写模型和失败后果完全不同：
  - 记忆库：每条都值钱，追加日志 + 快照，坏了要能回溯
  - 资料库：文档索引坏了无所谓，重建一遍就行（数据源是用户的 knowledge/ 目录）

表里只有文档和分块两样东西，检索走 FTS5 trigram + bm25 排序，
命中不到再退 LIKE 子串 —— 中文短词很常见，不能只靠分词索引。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_doc (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    path    TEXT UNIQUE NOT NULL,
    title   TEXT,
    mtime   REAL,
    chars   INTEGER DEFAULT 0,
    chunks  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kb_chunk (
    id     INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL,
    ord    INTEGER NOT NULL,
    text   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_doc ON kb_chunk(doc_id);
"""

# trigram 分词对中文最稳：不依赖词表，「磅礴」「的」这种都能命中
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
    text,
    tokenize = 'trigram'
);
"""


class KbStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self.fts = self._init_fts()

    def _init_fts(self) -> bool:
        try:
            with self._lock:
                self._conn.executescript(FTS_SCHEMA)
                self._conn.commit()
            return True
        except sqlite3.Error as exc:
            # 没编译 FTS5 的 SQLite 也有，退成 LIKE 还能用，只是排序差一点
            logger.warning("FTS5 不可用，资料库退化为 LIKE 匹配：%s", exc)
            return False

    # ------------------------------------------------------------ 写
    def doc_mtime(self, path: str) -> Optional[float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mtime FROM kb_doc WHERE path = ?", (path,)
            ).fetchall()
        return float(rows[0]["mtime"]) if rows else None

    def upsert_doc(self, path: str, title: str, mtime: float,
                   chars: int, chunks: int) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kb_doc(path, title, mtime, chars, chunks) VALUES (?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET title=excluded.title, "
                "mtime=excluded.mtime, chars=excluded.chars, chunks=excluded.chunks",
                (path, title, mtime, chars, chunks),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM kb_doc WHERE path = ?", (path,)
            ).fetchone()
        return int(row["id"]) if row else 0

    def replace_chunks(self, doc_id: int, chunks: List[str]) -> None:
        with self._lock:
            old = self._conn.execute(
                "SELECT id FROM kb_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchall()
            for row in old:
                if self.fts:
                    try:
                        self._conn.execute(
                            "DELETE FROM kb_fts WHERE rowid = ?", (row["id"],)
                        )
                    except sqlite3.Error:
                        pass
            self._conn.execute("DELETE FROM kb_chunk WHERE doc_id = ?", (doc_id,))
            self._conn.commit()
        self.add_chunks(doc_id, chunks)

    def add_chunks(self, doc_id: int, chunks: List[str]) -> None:
        with self._lock:
            for index, text in enumerate(chunks):
                cur = self._conn.execute(
                    "INSERT INTO kb_chunk(doc_id, ord, text) VALUES (?,?,?)",
                    (doc_id, index, text),
                )
                if self.fts:
                    cid = cur.lastrowid
                    try:
                        self._conn.execute(
                            "INSERT INTO kb_fts(rowid, text) VALUES (?, ?)", (cid, text)
                        )
                    except sqlite3.Error:
                        pass
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kb_chunk")
            self._conn.execute("DELETE FROM kb_doc")
            try:
                self._conn.execute("DELETE FROM kb_fts")
            except sqlite3.Error:
                pass
            self._conn.commit()

    def all_paths(self) -> List[str]:
        """已索引的全部文件路径。增量清理用：文件删了索引也得跟着删。"""
        with self._lock:
            rows = self._conn.execute("SELECT path FROM kb_doc").fetchall()
        return [str(row["path"]) for row in rows]

    def delete_doc(self, path: str) -> None:
        """按路径删掉一篇文档和它的所有分块。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM kb_doc WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                return
            doc_id = int(row["id"])
            if self.fts:
                ids = self._conn.execute(
                    "SELECT id FROM kb_chunk WHERE doc_id = ?", (doc_id,)
                ).fetchall()
                for item in ids:
                    try:
                        self._conn.execute(
                            "DELETE FROM kb_fts WHERE rowid = ?", (item["id"],)
                        )
                    except sqlite3.Error:
                        pass
            self._conn.execute("DELETE FROM kb_chunk WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM kb_doc WHERE id = ?", (doc_id,))
            self._conn.commit()

    # ------------------------------------------------------------ 读
    def search(self, query: str, limit: int = 4) -> List[Dict[str, Any]]:
        """命中即返回。查询会拆成若干词再「任一命中」。

        模型很爱一次丢进来「换码 运费 售后规定」这种组合词，
        整串去匹配必然零结果 —— 所以这里必须拆。

        Args:
            query: 检索词。
            limit: 最多返回几段。

        Returns:
            list[dict]: 每项含 title / text / path。
        """
        query = (query or "").strip()
        if not query:
            return []

        terms = [t for t in re.split(r"[\s,，、。;；/|]+", query) if len(t) >= 2]
        if not terms:
            terms = [query]
        terms = terms[:6]

        rows: List[sqlite3.Row] = []
        fts_terms = [t for t in terms if len(t) >= 3]
        if self.fts and fts_terms:
            expr = " OR ".join('"%s"' % t.replace('"', " ") for t in fts_terms)
            try:
                with self._lock:
                    rows = self._conn.execute(
                        "SELECT c.id AS cid, c.text, d.path, d.title "
                        "FROM kb_fts f JOIN kb_chunk c ON c.id = f.rowid "
                        "JOIN kb_doc d ON d.id = c.doc_id "
                        "WHERE kb_fts MATCH ? ORDER BY bm25(kb_fts) LIMIT ?",
                        (expr, limit * 2),
                    ).fetchall()
            except sqlite3.Error:
                rows = []

        if not rows:
            patterns = ["%" + t + "%" for t in terms]
            clause = " OR ".join(["c.text LIKE ?"] * len(patterns))
            sql = (
                "SELECT c.id AS cid, c.text, d.path, d.title "
                "FROM kb_chunk c JOIN kb_doc d ON d.id = c.doc_id "
                "WHERE " + clause + " LIMIT ?"
            )
            with self._lock:
                rows = self._conn.execute(sql, (*patterns, limit * 2)).fetchall()

        # 同一个文档连续命中就并起来读，别一段一段碎着给模型
        out: List[Dict[str, Any]] = []
        seen_chunks = set()
        per_doc: Dict[str, int] = {}
        for row in rows:
            cid = int(row["cid"])
            if cid in seen_chunks:
                continue
            budget = 2 if len(terms) > 1 else 1
            used = per_doc.get(row["path"], 0)
            if used >= budget and row["path"] not in [o["path"] for o in out[:len(terms)]]:
                continue
            seen_chunks.add(cid)
            per_doc[row["path"]] = used + 1
            out.append(
                {"title": row["title"], "path": row["path"], "text": row["text"]}
            )
            if len(out) >= limit:
                break
        return out

    def close(self) -> None:
        """释放连接。

        面板每个请求都可能新建一个 KbStore（web 模式下没有 Bot），
        没有 close 就会一直攒连接；Windows 上还会占着文件，
        连临时目录都删不掉。
        """
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def stats(self) -> Dict[str, int]:
        with self._lock:
            docs = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(chars),0) AS c FROM kb_doc"
            ).fetchone()
            chunks = self._conn.execute("SELECT COUNT(*) AS n FROM kb_chunk").fetchone()
        return {
            "docs": int(docs["n"]),
            "chars": int(docs["c"]),
            "chunks": int(chunks["n"]),
        }

    def list_docs(self, limit: int = 200) -> List[Dict[str, Any]]:
        """给后台面板用的文档清单（不含正文，只给元信息）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, title, mtime, chars, chunks FROM kb_doc "
                "ORDER BY mtime DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "path": str(row["path"]),
                "title": str(row["title"] or ""),
                "mtime": float(row["mtime"] or 0),
                "chars": int(row["chars"] or 0),
                "chunks": int(row["chunks"] or 0),
            }
            for row in rows
        ]
