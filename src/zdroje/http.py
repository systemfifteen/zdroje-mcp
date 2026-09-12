"""Shared HTTP client: per-host rate limiting, SQLite response cache, consistent headers."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import socket
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from . import db
from .config import Settings

log = logging.getLogger(__name__)


class BlockedTarget(Exception):
    """Raised when an outbound request targets a non-public / private address (SSRF guard)."""


def _assert_public_url(url: str) -> None:
    """Reject non-http(s) schemes and hosts resolving to private/loopback/link-local/reserved IPs.

    Blocks the token-holder `fetch` tool from reaching internal services (SSRF). Also used on each
    redirect hop. Legitimate sources (egov.banskabystrica.sk, dennikn.sk, …) are public → pass.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedTarget(f"blocked scheme: {parts.scheme or '(none)'}")
    host = parts.hostname
    if not host:
        raise BlockedTarget("blocked: no host in URL")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise BlockedTarget(f"DNS resolution failed for {host}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise BlockedTarget(f"blocked non-public target: {host} -> {ip}")


async def _redirect_guard(resp: "httpx.Response") -> None:
    """Validate each redirect hop before httpx follows it."""
    if resp.is_redirect:
        loc = resp.headers.get("location")
        if loc:
            _assert_public_url(str(resp.url.join(loc)))


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
            event_hooks={"response": [_redirect_guard]},
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

        _assert_public_url(full_url)
        await self._throttle(full_url)
        resp = await self.client.get(full_url, headers=headers)
        log.debug("GET %s -> %s", full_url, resp.status_code)
        out = Response(resp.status_code, resp.text, str(resp.url))
        if key is not None and out.ok:
            db.cache_put(self.conn, key, full_url, out.status, out.text, ttl)
        return out

    async def get_bytes(self, url: str, *, headers: dict | None = None, timeout: float | None = None) -> tuple[int, bytes, dict]:
        _assert_public_url(url)
        await self._throttle(url)
        resp = await self.client.get(url, headers=headers, timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT)
        log.debug("GET(bytes) %s -> %s (%d B)", url, resp.status_code, len(resp.content))
        return resp.status_code, resp.content, dict(resp.headers)

    async def post(self, url: str, *, data: dict, headers: dict | None = None) -> Response:
        _assert_public_url(url)
        await self._throttle(url)
        resp = await self.client.post(url, data=data, headers=headers)
        log.debug("POST %s -> %s", url, resp.status_code)
        return Response(resp.status_code, resp.text, str(resp.url))
