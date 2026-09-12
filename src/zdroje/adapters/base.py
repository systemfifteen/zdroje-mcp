"""Adapter protocol and shared result types."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..config import Settings
from ..extract import extract_article
from ..http import Http

log = logging.getLogger(__name__)

SEARCH_TTL = 3600          # 1 h
ARTICLE_TTL = 24 * 3600    # 24 h


class AdapterError(Exception):
    """Error attributable to one source; `code` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SearchResult:
    source: str
    title: str
    url: str
    published_at: str | None
    snippet: str = ""
    author: str | None = None
    paywalled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Article:
    source: str
    title: str | None
    url: str
    published_at: str | None
    author: str | None
    text_markdown: str
    paywalled: bool = False
    truncated: bool = False
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _host(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def parse_date_bound(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def in_range(published_at: str | None, since: date | None, until: date | None) -> bool:
    if since is None and until is None:
        return True
    if not published_at:
        return True  # unknown date: keep, let the caller judge
    try:
        d = date.fromisoformat(published_at[:10])
    except ValueError:
        return True
    if since and d < since:
        return False
    if until and d > until:
        return False
    return True


class Adapter:
    """Base class. Subclasses override `search` and optionally `fetch`."""

    def __init__(self, cfg: dict[str, Any], http: Http, settings: Settings):
        self.cfg = cfg
        self.http = http
        self.settings = settings
        self.id: str = cfg["id"]
        self.name: str = cfg.get("name", self.id)
        self.kind: str = cfg.get("kind", "news")
        self.base_url: str = cfg.get("base_url", "").rstrip("/")
        self.paywalled: bool = bool(cfg.get("paywalled", False))
        self.host = _host(self.base_url) if self.base_url else ""
        self.extra_hosts = [_host(h) for h in cfg.get("extra_hosts", [])]

    # --- capabilities ------------------------------------------------------

    @property
    def can_search(self) -> bool:
        return True

    @property
    def can_fetch(self) -> bool:
        return True

    def matches(self, url: str) -> bool:
        h = _host(url)
        return bool(self.host) and (h == self.host or h in self.extra_hosts)

    # --- operations --------------------------------------------------------

    async def search(self, query: str, since: date | None, until: date | None, limit: int) -> list[SearchResult]:
        raise NotImplementedError

    async def fetch(self, url: str, max_chars: int) -> Article:
        resp = await self.http.get(url, ttl=ARTICLE_TTL)
        if not resp.ok:
            raise AdapterError("http_error", f"{self.name} returned HTTP {resp.status}")
        return self.article_from_html(resp.text, resp.url, max_chars)

    # --- helpers -----------------------------------------------------------

    def article_from_html(self, html: str, url: str, max_chars: int, *, paywalled: bool = False) -> Article:
        body_selector = (self.cfg.get("article") or {}).get("body")
        ex = extract_article(html, url, body_selector=body_selector)
        text, truncated = _truncate(ex.text, max_chars)
        return Article(
            source=self.id,
            title=ex.title,
            url=url,
            published_at=ex.date,
            author=ex.author,
            text_markdown=text,
            paywalled=paywalled,
            truncated=truncated,
        )


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars and len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n\n[… skrátené]", True
    return text, False
