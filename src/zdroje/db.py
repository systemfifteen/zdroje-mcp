"""SQLite storage: MsZ document index (FTS5), HTTP cache, per-source status."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    body        TEXT NOT NULL,
    year        INTEGER NOT NULL,
    number      TEXT,
    date        TEXT NOT NULL,
    egov_row_id TEXT,
    UNIQUE(body, date, number)
);

CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    session_id     INTEGER NOT NULL REFERENCES sessions(id),
    doc_type       TEXT NOT NULL,
    label          TEXT,
    name           TEXT NOT NULL,
    published_at   TEXT,
    egov_arguments TEXT,
    size_text      TEXT,
    sha256         TEXT,
    local_path     TEXT,
    pages          INTEGER,
    indexed_at     TEXT,
    index_error    TEXT,
    parent_zip_id  INTEGER REFERENCES documents(id),
    UNIQUE(session_id, doc_type, name, parent_zip_id)
);

CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    UNIQUE(document_id, page_no)
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    text,
    content='pages',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO pages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key   TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    status      INTEGER NOT NULL,
    body        TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS source_status (
    source_id     TEXT PRIMARY KEY,
    last_ok_at    TEXT,
    last_error    TEXT,
    last_error_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


# --- source status ---------------------------------------------------------


def mark_ok(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        "INSERT INTO source_status(source_id, last_ok_at) VALUES (?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET last_ok_at=excluded.last_ok_at",
        (source_id, now_iso()),
    )
    conn.commit()


def mark_error(conn: sqlite3.Connection, source_id: str, message: str) -> None:
    conn.execute(
        "INSERT INTO source_status(source_id, last_error, last_error_at) VALUES (?, ?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET last_error=excluded.last_error, "
        "last_error_at=excluded.last_error_at",
        (source_id, message[:500], now_iso()),
    )
    conn.commit()


def get_status(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM source_status").fetchall()
    return {r["source_id"]: dict(r) for r in rows}


# --- http cache ------------------------------------------------------------


def cache_get(conn: sqlite3.Connection, key: str) -> tuple[int, str] | None:
    row = conn.execute(
        "SELECT status, body, fetched_at, ttl_seconds FROM http_cache WHERE cache_key=?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    fetched = datetime.fromisoformat(row["fetched_at"])
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    if age > row["ttl_seconds"]:
        conn.execute("DELETE FROM http_cache WHERE cache_key=?", (key,))
        conn.commit()
        return None
    return row["status"], row["body"]


def cache_put(conn: sqlite3.Connection, key: str, url: str, status: int, body: str, ttl: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO http_cache(cache_key, url, status, body, fetched_at, ttl_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (key, url, status, body, now_iso(), ttl),
    )
    conn.commit()


def cache_purge_expired(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM http_cache WHERE "
        "(julianday('now') - julianday(fetched_at)) * 86400 > ttl_seconds"
    )
    conn.commit()
    return cur.rowcount
