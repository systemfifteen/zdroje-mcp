"""WordPress sites with REST API disabled but the search RSS feed (/feed/?s=) available."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone

import feedparser

from ..extract import strip_tags
from .base import SEARCH_TTL, Adapter, AdapterError, SearchResult, in_range


def _entry_date(entry) -> str | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    ts = calendar.timegm(parsed)
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat()


class WordPressRssAdapter(Adapter):
    feed_path = "/feed/"

    async def _feed(self, params: dict) -> list:
        resp = await self.http.get(f"{self.base_url}{self.feed_path}", params=params, ttl=SEARCH_TTL)
        if not resp.ok:
            raise AdapterError("http_error", f"{self.name} feed returned HTTP {resp.status}")
        parsed = feedparser.parse(resp.text)
        if parsed.get("bozo") and not parsed.entries:
            raise AdapterError("bad_response", f"{self.name} feed could not be parsed")
        return parsed.entries

    def _to_result(self, entry) -> SearchResult:
        author = entry.get("author") or None
        return SearchResult(
            source=self.id,
            title=strip_tags(entry.get("title")),
            url=entry.get("link", ""),
            published_at=_entry_date(entry),
            snippet=strip_tags(entry.get("summary"))[:400],
            author=author,
            paywalled=self.paywalled,
        )

    async def search(self, query: str, since: date | None, until: date | None, limit: int) -> list[SearchResult]:
        results: list[SearchResult] = []
        seen: set[str] = set()
        page = 1
        while len(results) < limit and page <= 3:
            params = {"s": query}
            if page > 1:
                params["paged"] = page
            entries = await self._feed(params)
            if not entries:
                break
            for e in entries:
                r = self._to_result(e)
                if r.url in seen:
                    continue
                seen.add(r.url)
                if in_range(r.published_at, since, until):
                    results.append(r)
            if len(entries) < 10:
                break
            page += 1
        return results[:limit]
