"""Queries over the local MsZ document index (SQLite + FTS5)."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

DOC_TYPES = ("zapisnica", "uznesenia", "hlasovanie", "material", "pozvanka", "ine")
_TOKEN = re.compile(r"\w+", re.UNICODE)


def fts_query(user_query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression: quoted tokens, prefix match, implicit AND.

    A phrase in double quotes is kept as a phrase.
    """
    parts: list[str] = []
    for phrase in re.findall(r'"([^"]+)"', user_query):
        toks = _TOKEN.findall(phrase)
        if toks:
            parts.append('"' + " ".join(toks) + '"')
    rest = re.sub(r'"[^"]+"', " ", user_query)
    for tok in _TOKEN.findall(rest):
        if tok.isdigit() or len(tok) < 3:
            parts.append(f'"{tok}"')
        else:
            parts.append(f'"{tok}"*')
    return " ".join(parts) if parts else '""'


def doc_url(base_url: str, arguments: str | None) -> str | None:
    if not arguments:
        return None
    return f"{base_url}/FileOutputHttpHandler.ashx?arguments={arguments}"


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    year: int | None = None,
    doc_type: str | None = None,
    limit: int = 10,
    base_url: str = "",
) -> list[dict[str, Any]]:
    match = fts_query(query)
    sql = """
        SELECT p.id AS page_id, p.page_no, d.id AS doc_id, d.doc_type, d.name, d.label,
               d.parent_zip_id, s.date AS session_date, s.year, s.number AS session_number,
               COALESCE(parent.egov_arguments, d.egov_arguments) AS arguments,
               snippet(pages_fts, 0, '<b>', '</b>', '…', 24) AS snippet,
               bm25(pages_fts) AS rank
        FROM pages_fts
        JOIN pages p ON p.id = pages_fts.rowid
        JOIN documents d ON d.id = p.document_id
        LEFT JOIN documents parent ON parent.id = d.parent_zip_id
        JOIN sessions s ON s.id = d.session_id
        WHERE pages_fts MATCH ?
    """
    args: list[Any] = [match]
    if year is not None:
        sql += " AND s.year = ?"
        args.append(year)
    if doc_type:
        sql += " AND d.doc_type = ?"
        args.append(doc_type)
    sql += " ORDER BY rank LIMIT ?"
    args.append(max(1, min(limit, 50)))
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "doc_id": r["doc_id"],
                "doc_type": r["doc_type"],
                "doc_name": r["name"],
                "label": r["label"],
                "session_date": r["session_date"],
                "session_number": r["session_number"],
                "year": r["year"],
                "page": r["page_no"],
                "snippet": re.sub(r"\s+", " ", r["snippet"]).strip(),
                "source_url": doc_url(base_url, r["arguments"]),
            }
        )
    return out


def get_document(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    page_from: int | None = None,
    page_to: int | None = None,
    max_chars: int = 20000,
    base_url: str = "",
) -> dict[str, Any] | None:
    d = conn.execute(
        """SELECT d.*, s.date AS session_date, s.year, s.number AS session_number,
                  COALESCE(parent.egov_arguments, d.egov_arguments) AS arguments
           FROM documents d
           JOIN sessions s ON s.id = d.session_id
           LEFT JOIN documents parent ON parent.id = d.parent_zip_id
           WHERE d.id = ?""",
        (doc_id,),
    ).fetchone()
    if d is None:
        return None
    total = d["pages"] or 0
    lo = max(1, page_from or 1)
    hi = min(total, page_to or total) if total else (page_to or lo)
    rows = conn.execute(
        "SELECT page_no, text FROM pages WHERE document_id=? AND page_no BETWEEN ? AND ? ORDER BY page_no",
        (doc_id, lo, hi),
    ).fetchall()
    chunks: list[str] = []
    used = 0
    truncated = False
    last_page = None
    for r in rows:
        block = f"--- strana {r['page_no']} ---\n{r['text'].strip()}"
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > 200:
                chunks.append(block[:remaining].rstrip() + "\n[… skrátené]")
                last_page = r["page_no"]
            truncated = True
            break
        chunks.append(block)
        used += len(block) + 2
        last_page = r["page_no"]
    return {
        "doc_id": d["id"],
        "doc_type": d["doc_type"],
        "doc_name": d["name"],
        "label": d["label"],
        "session_date": d["session_date"],
        "session_number": d["session_number"],
        "year": d["year"],
        "published_at": d["published_at"],
        "total_pages": total,
        "page_from": lo,
        "page_to": last_page,
        "truncated": truncated,
        "index_error": d["index_error"],
        "source_url": doc_url(base_url, d["arguments"]),
        "text": "\n\n".join(chunks),
    }


def list_sessions(conn: sqlite3.Connection, year: int | None = None, *, base_url: str = "") -> list[dict[str, Any]]:
    sql = "SELECT * FROM sessions"
    args: list[Any] = []
    if year is not None:
        sql += " WHERE year = ?"
        args.append(year)
    sql += " ORDER BY date DESC"
    sessions = conn.execute(sql, args).fetchall()
    docs = conn.execute(
        """SELECT d.*, COALESCE(parent.egov_arguments, d.egov_arguments) AS arguments
           FROM documents d LEFT JOIN documents parent ON parent.id = d.parent_zip_id
           ORDER BY d.session_id, d.parent_zip_id NULLS FIRST, d.id"""
    ).fetchall()
    by_session: dict[int, list[dict]] = {}
    for d in docs:
        by_session.setdefault(d["session_id"], []).append(
            {
                "doc_id": d["id"],
                "doc_type": d["doc_type"],
                "name": d["name"],
                "label": d["label"],
                "published_at": d["published_at"],
                "pages": d["pages"],
                "indexed": d["indexed_at"] is not None and not d["index_error"],
                "index_error": d["index_error"],
                "in_zip": d["parent_zip_id"] is not None,
                "source_url": doc_url(base_url, d["arguments"]),
            }
        )
    return [
        {
            "session_id": s["id"],
            "body": s["body"],
            "year": s["year"],
            "number": s["number"],
            "date": s["date"],
            "documents": by_session.get(s["id"], []),
        }
        for s in sessions
    ]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """SELECT (SELECT COUNT(*) FROM sessions) AS sessions,
                  (SELECT COUNT(*) FROM documents) AS documents,
                  (SELECT COUNT(*) FROM documents WHERE indexed_at IS NOT NULL AND index_error IS NULL) AS indexed,
                  (SELECT COUNT(*) FROM documents WHERE index_error IS NOT NULL) AS failed,
                  (SELECT COUNT(*) FROM pages) AS pages,
                  (SELECT MAX(indexed_at) FROM documents) AS last_indexed_at,
                  (SELECT MIN(year) FROM sessions) AS first_year,
                  (SELECT MAX(year) FROM sessions) AS last_year"""
    ).fetchone()
    return dict(row) if row else {}
