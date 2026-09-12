"""Denník N: public search via RSS, article fetch with the owner's subscription cookie."""

from __future__ import annotations

from urllib.parse import urlsplit

from .base import ARTICLE_TTL, AdapterError, Article
from .wordpress_rss import WordPressRssAdapter

# Subscription cookie sa smie posielať IBA na dennikn.sk cez HTTPS (nie HTTP, nie iný host).
_COOKIE_HOSTS = {"dennikn.sk", "www.dennikn.sk", "e.dennikn.sk"}

# Strings that appear on a locked article when the reader is not recognised as a subscriber.
PAYWALL_MARKERS = (
    'class="n3_locked"',
    "n3_lock__hard",
    "e_lock__hard",
    "e_lock_button_login",
    "exkluzívnym obsahom pre predplatiteľov",
)
MIN_FULL_TEXT = 1500  # chars; locked teasers are shorter than this


class DennikNAdapter(WordPressRssAdapter):
    @property
    def cookie(self) -> str | None:
        return self.settings.dennikn_cookie

    def _looks_locked(self, html: str, text: str) -> bool:
        if len(text) >= MIN_FULL_TEXT:
            return False
        return any(marker in html for marker in PAYWALL_MARKERS)

    async def fetch(self, url: str, max_chars: int) -> Article:
        parts = urlsplit(url)
        cookie_ok = bool(self.cookie) and parts.scheme == "https" and (parts.hostname or "").lower() in _COOKIE_HOSTS
        headers = {"Cookie": self.cookie} if cookie_ok else None
        scope = "dennikn-auth" if cookie_ok else "dennikn-anon"
        resp = await self.http.get(url, headers=headers, ttl=ARTICLE_TTL, cache_scope=scope)
        if not resp.ok:
            raise AdapterError("http_error", f"Denník N returned HTTP {resp.status}")
        article = self.article_from_html(resp.text, resp.url, max_chars, paywalled=True)
        if self._looks_locked(resp.text, article.text_markdown):
            if self.cookie:
                raise AdapterError(
                    "cookie_expired",
                    "Denník N vrátil zamknutý článok napriek cookie. Obnov DENNIKN_COOKIE "
                    "(prihlás sa v prehliadači, skopíruj hodnotu Cookie hlavičky pre dennikn.sk "
                    "do Coolify secretu a reštartuj službu).",
                )
            raise AdapterError(
                "paywalled",
                "Článok je za paywallom a DENNIKN_COOKIE nie je nastavená; vrátený je len úvod.",
            )
        return article
