"""WordPress sites with the public REST API enabled (e.g. BBonline)."""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlsplit

from ..extract import html_fragment_to_markdown, strip_tags
from .base import ARTICLE_TTL, SEARCH_TTL, Adapter, AdapterError, Article, SearchResult, _truncate, in_range

FIELDS = "id,date,link,title,excerpt"
FIELDS_FULL = FIELDS + ",content"


class WordPressApiAdapter(Adapter):
    async def search(self, query: str, since: date | None, until: date | None, limit: int) -> list[SearchResult]:
        params: dict[str, str | int] = {
            "search": query,
            "per_page": min(max(limit, 1), 50),
            "orderby": "relevance",
            "_fields": FIELDS,
        }
        if since:
            params["after"] = f"{since.isoformat()}T00:00:00"
        if until:
            params["before"] = f"{until.isoformat()}T23:59:59"
        resp = await self.http.get(f"{self.base_url}/wp-json/wp/v2/posts", params=params, ttl=SEARCH_TTL)
        if not resp.ok:
            raise AdapterError("http_error", f"{self.name} WP API returned HTTP {resp.status}")
        try:
            posts = json.loads(resp.text)
        except json.JSONDecodeError as exc:
            raise AdapterError("bad_response", f"{self.name} WP API returned non-JSON") from exc
        if not isinstance(posts, list):
            raise AdapterError("bad_response", f"{self.name} WP API: {posts.get('message', 'unexpected payload')}")

        out: list[SearchResult] = []
        for p in posts:
            published = p.get("date")
            if not in_range(published, since, until):
                continue
            out.append(
                SearchResult(
                    source=self.id,
                    title=strip_tags(p.get("title", {}).get("rendered")),
                    url=p.get("link", ""),
                    published_at=published,
                    snippet=strip_tags(p.get("excerpt", {}).get("rendered"))[:400],
                    paywalled=self.paywalled,
                )
            )
        return out[:limit]

    async def fetch(self, url: str, max_chars: int) -> Article:
        slug = [s for s in urlsplit(url).path.split("/") if s]
        if slug:
            resp = await self.http.get(
                f"{self.base_url}/wp-json/wp/v2/posts",
                params={"slug": slug[-1], "_fields": FIELDS_FULL},
                ttl=ARTICLE_TTL,
            )
            if resp.ok:
                try:
                    posts = json.loads(resp.text)
                except json.JSONDecodeError:
                    posts = []
                if isinstance(posts, list) and posts:
                    p = posts[0]
                    text = html_fragment_to_markdown(p.get("content", {}).get("rendered", ""), url)
                    text, truncated = _truncate(text, max_chars)
                    return Article(
                        source=self.id,
                        title=strip_tags(p.get("title", {}).get("rendered")),
                        url=p.get("link") or url,
                        published_at=p.get("date"),
                        author=None,
                        text_markdown=text,
                        paywalled=self.paywalled,
                        truncated=truncated,
                    )
        # Fallback: plain HTML extraction (pages, non-post content types, API hiccups).
        return await super().fetch(url, max_chars)
