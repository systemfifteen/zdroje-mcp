"""Generic HTML search-results adapter driven by CSS selectors from sources.yaml.

Config example:
    search:
      path: "/?s={q}"
      item: "article"
      title: "h2 a"            # anchor: text = title, href = url
      date: "time"             # uses datetime attribute if present, else text
      snippet: ".excerpt"
      next: "a.next"           # optional pagination link
      url_pattern: "/clanky/"  # optional regex a result URL must match (filters sidebar/kino cards)
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import quote_plus, urljoin

from selectolax.parser import HTMLParser, Node

from .base import SEARCH_TTL, Adapter, AdapterError, SearchResult, in_range

_SK_DATE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
_SK_MONTHS = {
    "januára": 1, "februára": 2, "marca": 3, "apríla": 4, "mája": 5, "júna": 6,
    "júla": 7, "augusta": 8, "septembra": 9, "októbra": 10, "novembra": 11, "decembra": 12,
}
_SK_LONG = re.compile(r"(\d{1,2})\.\s*([a-záäčďéíľĺňóôŕšťúýž]+)\s*(\d{4})", re.I)


def parse_sk_date(text: str | None) -> str | None:
    """Normalise '8. 9. 2026', '08.09.2026', '8. septembra 2026' or ISO strings to ISO date."""
    if not text:
        return None
    text = text.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(microsecond=0).isoformat()
    except ValueError:
        pass
    m = _SK_DATE.search(text)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = _SK_LONG.search(text)
    if m and m.group(2).lower() in _SK_MONTHS:
        try:
            return date(int(m.group(3)), _SK_MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def _text(node: Node | None) -> str:
    return node.text(strip=True, separator=" ") if node is not None else ""


class HtmlSearchAdapter(Adapter):
    def _sel(self, key: str, default: str | None = None) -> str | None:
        return (self.cfg.get("search") or {}).get(key, default)

    async def search(self, query: str, since: date | None, until: date | None, limit: int) -> list[SearchResult]:
        path = self._sel("path", "/?s={q}")
        url = self.base_url + path.replace("{q}", quote_plus(query))
        results: list[SearchResult] = []
        seen: set[str] = set()
        pages = 0
        url_pattern = re.compile(self._sel("url_pattern")) if self._sel("url_pattern") else None
        while url and len(results) < limit and pages < 3:
            resp = await self.http.get(url, ttl=SEARCH_TTL)
            if not resp.ok:
                raise AdapterError("http_error", f"{self.name} search returned HTTP {resp.status}")
            tree = HTMLParser(resp.text)
            items = tree.css(self._sel("item", "article"))
            if not items:
                break
            for item in items:
                a = item.css_first(self._sel("title", "h2 a"))
                if a is None:
                    continue
                href = a.attributes.get("href")
                if not href:
                    continue
                link = urljoin(resp.url, href)
                if link in seen or (url_pattern and not url_pattern.search(link)):
                    continue
                seen.add(link)
                date_sel = self._sel("date")
                date_node = item.css_first(date_sel) if date_sel else None
                raw_date = None
                if date_node is not None:
                    raw_date = date_node.attributes.get("datetime") or _text(date_node)
                snippet_sel = self._sel("snippet")
                snippet = _text(item.css_first(snippet_sel)) if snippet_sel else ""
                r = SearchResult(
                    source=self.id,
                    title=_text(a) or link,
                    url=link,
                    published_at=parse_sk_date(raw_date),
                    snippet=snippet[:400],
                    paywalled=self.paywalled,
                )
                if in_range(r.published_at, since, until):
                    results.append(r)
            pages += 1
            next_sel = self._sel("next")
            nxt = tree.css_first(next_sel) if next_sel else None
            url = urljoin(resp.url, nxt.attributes.get("href", "")) if nxt is not None and nxt.attributes.get("href") else None
        return results[:limit]
