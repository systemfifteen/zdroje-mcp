"""MsZ indexer: eGOV session grid -> download PDF/ZIP -> pdftotext -> SQLite FTS5.

Usage:
    python -m zdroje.indexer                # current + previous year
    python -m zdroje.indexer --all          # every year the portal offers
    python -m zdroje.indexer --years 2018-2022
    python -m zdroje.indexer --year 2026 --force
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import logging
import re
import shutil
import sqlite3
import subprocess
import zipfile
from datetime import date
from pathlib import Path

from . import db
from .adapters import AdapterError
from .adapters.egov import DocRef, EgovAdapter, Session
from .config import Settings, load_settings
from .registry import Registry

log = logging.getLogger("zdroje.indexer")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", name.strip())
    return cleaned.strip("._") or "document"


# --- text extraction -------------------------------------------------------


def pdf_pages(path: Path) -> list[str]:
    """Per-page text. Prefers poppler's pdftotext -layout; falls back to pypdf."""
    if shutil.which("pdftotext"):
        proc = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            timeout=300,
        )
        if proc.returncode == 0:
            text = proc.stdout.decode("utf-8", errors="replace")
            pages = text.split("\f")
            if pages and not pages[-1].strip():
                pages.pop()
            return pages
        log.warning("pdftotext failed for %s: %s", path.name, proc.stderr.decode(errors="replace")[:200])
    try:
        from pypdf import PdfReader  # optional fallback, mainly for local dev without poppler
    except ImportError:
        raise RuntimeError("neither pdftotext nor pypdf available")
    reader = PdfReader(str(path))
    return [(p.extract_text() or "") for p in reader.pages]


def docx_pages(path: Path, chunk_chars: int = 3000) -> list[str]:
    """Paragraph text from word/document.xml, split into pseudo-pages of ~chunk_chars."""
    import xml.etree.ElementTree as ET

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    paragraphs: list[str] = []
    for p in root.iter(f"{{{ns['w']}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{ns['w']}}}t")).strip()
        if text:
            paragraphs.append(text)
    pages: list[str] = []
    buf: list[str] = []
    size = 0
    for para in paragraphs:
        if size + len(para) > chunk_chars and buf:
            pages.append("\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 1
    if buf:
        pages.append("\n".join(buf))
    return pages


def extract_pages(path: Path, name: str) -> list[str] | None:
    """Dispatch by file type. Returns None for unsupported formats."""
    lower = name.lower()
    if lower.endswith(".pdf"):
        return pdf_pages(path)
    if lower.endswith(".docx"):
        return docx_pages(path)
    return None


# --- persistence -----------------------------------------------------------


def upsert_session(conn: sqlite3.Connection, s: Session) -> int:
    conn.execute(
        """INSERT INTO sessions(body, year, number, date, egov_row_id) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(body, date, number) DO UPDATE SET egov_row_id=excluded.egov_row_id""",
        (s.body, s.year, s.number, s.date, s.row_id),
    )
    row = conn.execute(
        "SELECT id FROM sessions WHERE body=? AND date=? AND number=?", (s.body, s.date, s.number)
    ).fetchone()
    return int(row["id"])


def upsert_document(conn: sqlite3.Connection, session_id: int, d: DocRef, parent_zip_id: int | None = None) -> sqlite3.Row:
    """Insert or refresh one document row. Identity = (session, type, name, parent ZIP).

    Explicit select-then-write instead of ON CONFLICT: SQLite's UNIQUE treats NULL parent_zip_id as
    distinct, so a conflict clause never fires for top-level documents.
    """
    existing = conn.execute(
        "SELECT id FROM documents WHERE session_id=? AND doc_type=? AND name=? AND parent_zip_id IS ? "
        "ORDER BY id LIMIT 1",
        (session_id, d.doc_type, d.name, parent_zip_id),
    ).fetchone()
    if existing is None:
        cur = conn.execute(
            """INSERT INTO documents(session_id, doc_type, label, name, published_at, egov_arguments, size_text, parent_zip_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, d.doc_type, d.label, d.name, d.published_at, d.arguments, d.size_text, parent_zip_id),
        )
        doc_id = cur.lastrowid
    else:
        doc_id = existing["id"]
        conn.execute(
            "UPDATE documents SET label=?, published_at=?, egov_arguments=?, size_text=? WHERE id=?",
            (d.label, d.published_at, d.arguments, d.size_text, doc_id),
        )
    return conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()


def store_pages(conn: sqlite3.Connection, doc_id: int, pages: list[str], local_path: Path | None, sha: str | None) -> None:
    conn.execute("DELETE FROM pages WHERE document_id=?", (doc_id,))
    non_empty = 0
    for i, text in enumerate(pages, start=1):
        text = text.strip()
        if text:
            non_empty += 1
            conn.execute("INSERT INTO pages(document_id, page_no, text) VALUES (?, ?, ?)", (doc_id, i, text))
    error = None if non_empty else "no_text_layer"
    conn.execute(
        "UPDATE documents SET pages=?, local_path=?, sha256=?, indexed_at=?, index_error=? WHERE id=?",
        (len(pages), str(local_path) if local_path else None, sha, db.now_iso(), error, doc_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, doc_id: int, error: str) -> None:
    conn.execute(
        "UPDATE documents SET indexed_at=?, index_error=? WHERE id=?", (db.now_iso(), error[:300], doc_id)
    )
    conn.commit()


# --- pipeline --------------------------------------------------------------


class Indexer:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, registry: Registry):
        self.settings = settings
        self.conn = conn
        self.registry = registry
        egov = registry.egov
        if egov is None:
            raise RuntimeError("no egov source configured in sources.yaml")
        self.egov: EgovAdapter = egov
        self.stats = {"sessions": 0, "documents_seen": 0, "downloaded": 0, "indexed": 0, "failed": 0, "skipped": 0}

    async def run(self, years: list[int], force: bool = False) -> dict:
        for year in years:
            try:
                sessions = await self.egov.list_sessions(year)
            except AdapterError as exc:
                log.error("year %s: %s", year, exc.message)
                db.mark_error(self.conn, self.egov.id, exc.message)
                continue
            log.info("year %s: %d sessions", year, len(sessions))
            for s in sessions:
                await self._process_session(s, force)
            db.mark_ok(self.conn, self.egov.id)
        return self.stats

    async def _process_session(self, s: Session, force: bool) -> None:
        session_id = upsert_session(self.conn, s)
        self.stats["sessions"] += 1
        for d in s.docs:
            self.stats["documents_seen"] += 1
            row = upsert_document(self.conn, session_id, d)
            self.conn.commit()
            if row["indexed_at"] and not force and not (row["index_error"] or "").startswith("download_failed"):
                self.stats["skipped"] += 1
                continue
            await self._process_document(int(row["id"]), s, d)

    async def _process_document(self, doc_id: int, s: Session, d: DocRef) -> None:
        log.info("  %s %s (%s) %s", s.date, d.doc_type, d.label, d.name)
        try:
            content, ctype = await self.egov.download(d.url)
        except Exception as exc:  # network errors must not abort the run
            reason = f"{type(exc).__name__}: {exc}".strip(": ")
            log.warning("    download failed: %s", reason)
            mark_failed(self.conn, doc_id, f"download_failed: {reason}")
            self.stats["failed"] += 1
            return
        self.stats["downloaded"] += 1
        sha = hashlib.sha256(content).hexdigest()
        target_dir = self.settings.raw_dir / str(s.year) / s.date
        target_dir.mkdir(parents=True, exist_ok=True)
        local = target_dir / safe_name(d.name)
        local.write_bytes(content)

        lower = d.name.lower()
        is_pdf = lower.endswith(".pdf") or (ctype or "").startswith("application/pdf") or content[:4] == b"%PDF"
        is_docx = lower.endswith(".docx")
        is_zip = not is_docx and (lower.endswith(".zip") or (content[:2] == b"PK" and not lower.endswith((".xlsx", ".pptx"))))
        try:
            if is_pdf or is_docx:
                pages = extract_pages(local, "x.pdf" if is_pdf else d.name)
                store_pages(self.conn, doc_id, pages or [], local, sha)
                self.stats["indexed"] += 1
            elif is_zip:
                self._process_zip(doc_id, s, d, local, sha, content)
            else:
                mark_failed(self.conn, doc_id, f"unsupported_format: {lower.rsplit('.', 1)[-1]}")
                self.stats["failed"] += 1
        except Exception as exc:
            log.exception("    indexing failed for %s", d.name)
            mark_failed(self.conn, doc_id, f"index_failed: {exc}")
            self.stats["failed"] += 1

    def _process_zip(self, zip_doc_id: int, s: Session, d: DocRef, local: Path, sha: str, content: bytes) -> None:
        session_id = int(self.conn.execute("SELECT session_id FROM documents WHERE id=?", (zip_doc_id,)).fetchone()[0])
        extract_dir = local.with_suffix("")
        extract_dir.mkdir(exist_ok=True)
        members_indexed = 0
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                member_name = Path(info.filename).name
                if member_name.startswith("~$") or "__MACOSX" in info.filename or member_name.startswith("."):
                    continue  # Word lock files, macOS resource forks, hidden files
                child_ref = DocRef(
                    doc_type=d.doc_type, label=d.label, name=member_name, published_at=d.published_at,
                    size_text=None, arguments=d.arguments, url=d.url,
                )
                child = upsert_document(self.conn, session_id, child_ref, parent_zip_id=zip_doc_id)
                child_id = int(child["id"])
                if child["indexed_at"] and not child["index_error"]:
                    continue  # already indexed on a previous run
                if not member_name.lower().endswith((".pdf", ".docx")):
                    mark_failed(self.conn, child_id, f"unsupported_format: {member_name.rsplit('.', 1)[-1].lower()}")
                    continue
                out = extract_dir / safe_name(member_name)
                out.write_bytes(zf.read(info))
                try:
                    pages = extract_pages(out, member_name) or []
                    store_pages(self.conn, child_id, pages, out, hashlib.sha256(out.read_bytes()).hexdigest())
                    members_indexed += 1
                except Exception as exc:
                    mark_failed(self.conn, child_id, f"index_failed: {exc}")
        # The ZIP itself holds no text; mark it indexed so it is not re-downloaded.
        self.conn.execute(
            "UPDATE documents SET pages=0, local_path=?, sha256=?, indexed_at=?, index_error=NULL WHERE id=?",
            (str(local), sha, db.now_iso(), zip_doc_id),
        )
        self.conn.commit()
        self.stats["indexed"] += members_indexed


# --- CLI -------------------------------------------------------------------


def parse_years(args: argparse.Namespace, available: list[int]) -> list[int]:
    if args.all:
        return available
    if args.years:
        lo, _, hi = args.years.partition("-")
        lo_i, hi_i = int(lo), int(hi or lo)
        return [y for y in range(lo_i, hi_i + 1)]
    if args.year:
        return [args.year]
    today = date.today().year
    return [y for y in (today - 1, today) if y in available] or [today]


async def amain(args: argparse.Namespace) -> int:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = db.connect(settings.db_path)
    registry = Registry(settings, conn)
    try:
        indexer = Indexer(settings, conn, registry)
        available = await indexer.egov.list_years()
        years = parse_years(args, available)
        log.info("indexing years %s (portal offers %s)", years, available)
        stats = await indexer.run(years, force=args.force)
        purged = db.cache_purge_expired(conn)
        log.info("done: %s; purged %d expired cache rows", stats, purged)
        return 0 if stats["failed"] == 0 else 1
    finally:
        await registry.aclose()
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Index MsZ documents from eGOV into SQLite FTS5")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="all years the portal offers")
    g.add_argument("--years", help="range, e.g. 2018-2022")
    g.add_argument("--year", type=int)
    p.add_argument("--force", action="store_true", help="re-download and re-index already indexed documents")
    args = p.parse_args()
    raise SystemExit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
