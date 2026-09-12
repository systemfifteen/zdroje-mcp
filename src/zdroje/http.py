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


# Ranges that are not globally routable but that `is_private`/`is_global` miss on some
# interpreters (belt-and-suspenders alongside the `not is_global` check below).
_EXTRA_BLOCKED_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 CGNAT / shared address space
    ipaddress.ip_network("198.18.0.0/15"),  # RFC 2544 benchmarking
    ipaddress.ip_network("192.0.0.0/24"),   # RFC 6890 IETF protocol assignments
)


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if an outbound request must never reach this IP (SSRF guard)."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:  # unwrap ::ffff:127.0.0.1 → 127.0.0.1
        ip = mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified or not ip.is_global):
        return True
    return any(ip in net for net in _EXTRA_BLOCKED_NETS)


def _resolve_public(host: str, port: int) -> str:
    """Resolve `host`, require EVERY resolved address to be public, and return one to pin to.

    Returning the checked IP lets the transport connect to exactly that address, closing the
    DNS-rebinding TOCTOU (our check and httpx's own connect-time lookup could otherwise differ).
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise BlockedTarget(f"DNS resolution failed for {host}: {exc}") from exc
    pinned: str | None = None
    for info in infos:
        addr = info[4][0]
        if _ip_blocked(ipaddress.ip_address(addr)):
            raise BlockedTarget(f"blocked non-public target: {host} -> {addr}")
        if pinned is None:
            pinned = addr
    if pinned is None:
        raise BlockedTarget(f"no addresses for {host}")
    return pinned


def _assert_public_url(url: str) -> None:
    """Reject non-http(s) schemes and hosts that resolve to any non-public IP (early friendly check).

    The authoritative enforcement is `_PinnedTransport`; this runs first for a clear error and to
    validate the scheme before throttling. Legit sources (egov.banskabystrica.sk, dennikn.sk) pass.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedTarget(f"blocked scheme: {parts.scheme or '(none)'}")
    host = parts.hostname
    if not host:
        raise BlockedTarget("blocked: no host in URL")
    _resolve_public(host, parts.port or (443 if parts.scheme == "https" else 80))


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """Resolve + validate the host and pin the TCP connection to the checked IP.

    Runs on every hop (including redirects). Without pinning, our getaddrinfo check and httpx's own
    connect-time lookup can differ, letting a malicious resolver point the second lookup at
    127.0.0.1 / 169.254.169.254 (DNS rebinding). We connect to the validated IP but keep the
    original hostname for the Host header, TLS SNI and certificate verification, then restore the
    URL so redirect resolution and `response.url` stay correct.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise BlockedTarget(f"blocked scheme: {url.scheme or '(none)'}")
        host = url.host
        if not host:
            raise BlockedTarget("blocked: no host in URL")
        port = url.port or (443 if url.scheme == "https" else 80)
        pinned_ip = _resolve_public(host, port)
        request.extensions["sni_hostname"] = host  # TLS SNI + cert check against the real host
        request.url = url.copy_with(host=pinned_ip)  # connect to the exact validated IP
        try:
            return await super().handle_async_request(request)
        finally:
            request.url = url  # restore for redirect resolution / response.url


async def _redirect_guard(resp: "httpx.Response") -> None:
    """Validate each redirect hop before httpx follows it (defence in depth atop the transport)."""
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
            transport=_PinnedTransport(),
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
