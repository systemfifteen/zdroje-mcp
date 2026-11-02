"""Read-only access to the owner's Obsidian vault (bind-mounted into the container, e.g. at /vault).

Security model: the vault root is fixed by configuration; every requested path is resolved and must stay
inside it (no absolute paths, no `..`, no hidden segments, symlinks may not escape). Only Markdown notes
are served. Optional VAULT_EXCLUDE prefixes hide whole folders. Nothing here ever writes.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

# Notes shown by vault_start, in this order, if they exist (no leading underscore: those do not sync).
START_NOTES = ("AGENTS.md", "00 - Prehľad projektov.md")
NOTE_SUFFIX = ".md"
MAX_READ_CHARS = 100_000
WIKILINK = re.compile(r"!?\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")


class VaultError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def fold(text: str) -> str:
    """Case- and diacritics-insensitive form for matching ("Prehľad" ~ "prehlad")."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _target(m: re.Match) -> str:
    """Wikilink target; inside Markdown tables the alias pipe is escaped as \\|, so drop a trailing backslash."""
    return m.group(1).strip().rstrip("\\").strip()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="minutes")


@dataclass(frozen=True)
class Note:
    rel: str  # POSIX path relative to the vault root, e.g. "Projekty/Zdroje MCP.md"
    path: Path
    mtime: float
    size: int

    @property
    def title(self) -> str:
        return PurePosixPath(self.rel).stem


class Vault:
    def __init__(self, root: Path, exclude: tuple[str, ...] = ()):
        self.root = root.resolve()
        self.exclude = tuple(
            unicodedata.normalize("NFC", e.strip().strip("/")) for e in exclude if e.strip().strip("/")
        )

    # --- path handling -------------------------------------------------------

    def _excluded(self, rel: str) -> bool:
        return any(rel == e or rel.startswith(e + "/") for e in self.exclude)

    def _check_rel(self, rel: str) -> str:
        rel = unicodedata.normalize("NFC", rel.replace("\\", "/").strip())
        if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            raise VaultError("bad_path", "path must be relative to the vault root")
        parts = PurePosixPath(rel).parts
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            raise VaultError("bad_path", "path may not contain '.', '..' or hidden segments")
        return "/".join(parts)

    def _inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def notes(self) -> list[Note]:
        out: list[Note] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            base = Path(dirpath)
            rel_dir = base.relative_to(self.root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".")
                and not self._excluded(unicodedata.normalize("NFC", f"{rel_dir}/{d}".lstrip("/")))
            )
            for name in sorted(filenames):
                if name.startswith(".") or not name.endswith(NOTE_SUFFIX):
                    continue
                rel = unicodedata.normalize("NFC", f"{rel_dir}/{name}".lstrip("/"))
                path = base / name
                if self._excluded(rel) or not self._inside(path):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                out.append(Note(rel, path, st.st_mtime, st.st_size))
        return out

    def resolve(self, ref: str) -> Note:
        """Accepts a vault-relative path ("Projekty/X.md"), with or without .md, or a bare note name ("X")."""
        rel = self._check_rel(ref)
        if not rel.endswith(NOTE_SUFFIX):
            rel += NOTE_SUFFIX
        all_notes = self.notes()
        by_rel = {n.rel: n for n in all_notes}
        if rel in by_rel:
            return by_rel[rel]
        if "/" not in rel:  # bare name, like a wikilink
            wanted = fold(PurePosixPath(rel).stem)
            matches = [n for n in all_notes if fold(n.title) == wanted]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise VaultError("ambiguous", "several notes share this name: " + ", ".join(m.rel for m in matches))
        raise VaultError("not_found", f"no note '{ref}' in the vault (see vault_start for the tree)")

    # --- tools -------------------------------------------------------------

    def start(self, recent: int = 10) -> dict[str, Any]:
        notes = self.notes()
        by_rel = {n.rel: n for n in notes}
        start_notes = []
        for name in START_NOTES:
            n = by_rel.get(name)
            if n:
                start_notes.append({"path": n.rel, "modified": _iso(n.mtime), "content": n.path.read_text(encoding="utf-8", errors="replace")})
        folders: dict[str, list[str]] = {}
        for n in notes:
            folder = str(PurePosixPath(n.rel).parent)
            folders.setdefault("" if folder == "." else folder, []).append(n.title)
        newest = sorted(notes, key=lambda n: n.mtime, reverse=True)[: max(0, min(recent, 50))]
        return {
            "vault": self.root.name,
            "note_count": len(notes),
            "start_notes": start_notes,
            "tree": [{"folder": f or "/", "notes": t} for f, t in sorted(folders.items())],
            "recently_modified": [{"path": n.rel, "modified": _iso(n.mtime)} for n in newest],
            "hint": "Read notes with vault_read(path or note name); search with vault_search(query).",
        }

    def search(self, query: str, folder: str | None = None, limit: int = 20, snippets: int = 3) -> dict[str, Any]:
        terms = [fold(t) for t in re.findall(r"\w[\w.\-]*", query) if len(t) > 1]
        if not terms:
            raise VaultError("bad_query", "query needs at least one word of two or more characters")
        limit = max(1, min(limit, 50))
        prefix = self._check_rel(folder) + "/" if folder else ""
        hits = []
        for n in self.notes():
            if prefix and not n.rel.startswith(prefix):
                continue
            try:
                text = n.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            folded_title, folded_text = fold(n.rel), fold(text)
            if not all(t in folded_title or t in folded_text for t in terms):
                continue
            score = sum(5 * folded_title.count(t) + folded_text.count(t) for t in terms)
            lines = []
            for i, line in enumerate(text.splitlines(), start=1):
                if any(t in fold(line) for t in terms):
                    lines.append({"line": i, "text": line.strip()[:240]})
                    if len(lines) >= snippets:
                        break
            hits.append({"path": n.rel, "title": n.title, "modified": _iso(n.mtime), "score": score, "matches": lines})
        hits.sort(key=lambda h: (h["score"], h["modified"]), reverse=True)
        return {"query": query, "total": len(hits), "hits": hits[:limit]}

    def read(self, ref: str, start_line: int | None = None, end_line: int | None = None, max_chars: int = 30_000) -> dict[str, Any]:
        note = self.resolve(ref)
        text = note.path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        lo = max(1, start_line or 1)
        hi = min(len(lines), end_line or len(lines))
        body = "\n".join(lines[lo - 1 : hi]) if lines else ""
        max_chars = max(1000, min(max_chars, MAX_READ_CHARS))
        truncated = len(body) > max_chars
        links = sorted({_target(m) for m in WIKILINK.finditer(text)})
        title_f, rel_f = fold(note.title), fold(note.rel[: -len(NOTE_SUFFIX)])
        backlinks = []
        for other in self.notes():
            if other.rel == note.rel:
                continue
            try:
                other_text = other.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in WIKILINK.finditer(other_text):
                target = fold(_target(m))
                if target in (title_f, rel_f) or target.endswith("/" + title_f):
                    backlinks.append(other.rel)
                    break
        return {
            "path": note.rel,
            "title": note.title,
            "modified": _iso(note.mtime),
            "total_lines": len(lines),
            "lines": [lo, hi],
            "truncated": truncated,
            "content": body[:max_chars],
            "links": links,
            "backlinks": backlinks,
        }
