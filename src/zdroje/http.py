"""Shared HTTP client: per-host rate limiting, SQLite response cache, consistent headers."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from . import db
from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class Response:
    status: int
    text: str
    url: str
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Http:
    def __init__(self, settings: Settings, conn: sqlite3.Connection):
        self.settings = settings
        self.conn = conn
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout),
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "sk,cs;q=0.8,en;q=0.5",
            },
        )
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = {}
        self._min_gap = 1.0 / settings.rate_limit_per_host if settings.rate_limit_per_host > 0 else 0.0

    async def aclose(self) -> None:
        await self.client.aclose()

    # --- rate limiting -----------------------------------------------------

    async def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        async with self._locks[host]:
            last = self._last_request.get(host, 0.0)
            wait = self._min_gap - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()

    # --- public API --------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        ttl: int = 0,
        cache_scope: str = "",
    ) -> Response:
        """GET with optional caching. `cache_scope` separates e.g. authenticated and anonymous copies."""
        full_url = str(httpx.URL(url, params=params)) if params else url
        key = None
        if ttl > 0:
            key = hashlib.sha256(f"{cache_scope}|{full_url}".encode()).hexdigest()
            hit = db.cache_get(self.conn, key)
            if hit is not None:
                return Response(hit[0], hit[1], full_url, from_cache=True)

        await self._throttle(full_url)
        resp = await self.client.get(full_url, headers=headers)
        log.debug("GET %s -> %s", full_url, resp.status_code)
        out = Response(resp.status_code, resp.text, str(resp.url))
        if key is not None and out.ok:
            db.cache_put(self.conn, key, full_url, out.status, out.text, ttl)
        return out

    async def get_bytes(self, url: str, *, headers: dict | None = None, timeout: float | None = None) -> tuple[int, bytes, dict]:
        await self._throttle(url)
        resp = await self.client.get(url, headers=headers, timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT)
        log.debug("GET(bytes) %s -> %s (%d B)", url, resp.status_code, len(resp.content))
        return resp.status_code, resp.content, dict(resp.headers)

    async def post(self, url: str, *, data: dict, headers: dict | None = None) -> Response:
        await self._throttle(url)
        resp = await self.client.post(url, data=data, headers=headers)
        log.debug("POST %s -> %s", url, resp.status_code)
        return Response(resp.status_code, resp.text, str(resp.url))
