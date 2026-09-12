"""MCP server (Streamable HTTP) exposing news search/fetch and the MsZ document index.

Run locally:  uvicorn zdroje.server:app --host 0.0.0.0 --port 8000
Client URL:   https://<host>/mcp/<MCP_TOKEN>   or   https://<host>/mcp with Authorization: Bearer <MCP_TOKEN>
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import date
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import db, index
from .adapters import Adapter, AdapterError
from .adapters.base import parse_date_bound
from .config import load_settings
from .registry import Registry

settings = load_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("zdroje.server")

conn = db.connect(settings.db_path)
registry = Registry(settings, conn)

# Bystricoviny's WordPress search alone takes ~8 s server-side (measured 2026-09-12); results are cached 1 h.
PER_SOURCE_TIMEOUT = 15.0
GENERIC_WEB = Adapter({"id": "web", "name": "web", "kind": "news"}, registry.http, settings)

INSTRUCTIONS = """Zdroje MCP: Banská Bystrica news sources and city council (MsZ) documents.

Tools:
- list_sources: registry + health of each source.
- search: query local news (bbonline, bystricoviny, bystricak, dennikn) and optionally the MsZ index (source id "msz").
- fetch: full article text in Markdown for a URL (Denník N uses the owner's subscription cookie).
- msz_search / msz_document / msz_sessions: full-text search over indexed council documents
  (zápisnice, uznesenia, hlasovania, materiály) from egov.banskabystrica.sk, 2018 onwards.

Cite with the returned url / source_url. Quote paywalled text briefly.
"""

mcp = FastMCP(
    "zdroje",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    # Za Traefikom (bez host portu) + TokenAuth pred /mcp; DNS-rebinding ochranu
    # netreba a jej auto-zapnutie pre default host 127.0.0.1 blokuje produkčný Host.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def _egov_base() -> str:
    egov = registry.egov
    return egov.base_url if egov else ""


# --- tools -----------------------------------------------------------------


@mcp.tool()
async def list_sources() -> dict[str, Any]:
    """List configured sources with capabilities and last known health, plus MsZ index statistics."""
    status = db.get_status(conn)
    out = []
    for a in registry.adapters.values():
        if a.kind == "council":
            caps = ["msz_search", "msz_document", "msz_sessions"]
        else:
            caps = [c for c, ok in (("search", a.can_search), ("fetch", a.can_fetch)) if ok]
        entry: dict[str, Any] = {
            "id": a.id,
            "name": a.name,
            "kind": a.kind,
            "base_url": a.base_url,
            "capabilities": caps,
            "paywalled": a.paywalled,
            "status": status.get(a.id, {}),
        }
        if a.id == "dennikn":
            entry["cookie_configured"] = bool(settings.dennikn_cookie)
        if a.kind == "council":
            entry["index"] = index.stats(conn)
        out.append(entry)
    return {"sources": out}


async def _search_one(adapter: Adapter, query: str, since: date | None, until: date | None, limit: int) -> list[dict]:
    results = await asyncio.wait_for(adapter.search(query, since, until, limit), timeout=PER_SOURCE_TIMEOUT)
    return [r.to_dict() for r in results]


@mcp.tool()
async def search(
    query: str,
    sources: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search Banská Bystrica news sources in parallel and merge results (newest first).

    Args:
        query: free text, Slovak with diacritics is fine.
        sources: subset of source ids (see list_sources); include "msz" to also search council documents. Default: all news sources.
        since / until: ISO dates (YYYY-MM-DD) to bound publication date.
        limit: max results per source (1-50).
    """
    limit = max(1, min(limit, 50))
    since_d, until_d = parse_date_bound(since), parse_date_bound(until)
    wanted = set(sources) if sources else {a.id for a in registry.news}
    unknown = [s for s in wanted if s not in registry.adapters]
    adapters = [a for a in registry.news if a.id in wanted]
    include_msz = "msz" in wanted and registry.egov is not None

    tasks = {a.id: asyncio.create_task(_search_one(a, query, since_d, until_d, limit)) for a in adapters}
    results: list[dict] = []
    errors: list[dict] = [{"source": s, "code": "unknown_source", "message": "not configured"} for s in unknown]
    for source_id, task in tasks.items():
        try:
            results.extend(await task)
            db.mark_ok(conn, source_id)
        except AdapterError as exc:
            errors.append({"source": source_id, "code": exc.code, "message": exc.message})
            db.mark_error(conn, source_id, exc.message)
        except asyncio.TimeoutError:
            errors.append({"source": source_id, "code": "timeout", "message": f"no answer within {PER_SOURCE_TIMEOUT:.0f}s"})
            db.mark_error(conn, source_id, "timeout")
        except Exception as exc:  # one broken source must not break the tool
            log.exception("search failed for %s", source_id)
            errors.append({"source": source_id, "code": "error", "message": str(exc)[:200]})
            db.mark_error(conn, source_id, str(exc))

    seen: set[str] = set()
    deduped = []
    for r in results:
        key = _canonical(r["url"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    deduped.sort(key=lambda r: (r.get("published_at") or ""), reverse=True)

    out: dict[str, Any] = {"query": query, "results": deduped, "errors": errors, "sources_queried": [a.id for a in adapters]}
    if include_msz:
        year = since_d.year if since_d and until_d and since_d.year == until_d.year else None
        out["msz_results"] = index.search(conn, query, year=year, limit=limit, base_url=_egov_base())
        out["sources_queried"].append("msz")
    return out


@mcp.tool()
async def fetch(url: str, max_chars: int = 20000) -> dict[str, Any]:
    """Fetch one article and return its text as Markdown with title, date and author.

    Works for any http(s) URL; configured sources get source-specific extraction. Denník N articles are
    fetched with the owner's subscription cookie; a `cookie_expired` error means the cookie must be refreshed.
    """
    adapter = registry.for_url(url) or GENERIC_WEB
    try:
        article = await asyncio.wait_for(adapter.fetch(url, max_chars), timeout=PER_SOURCE_TIMEOUT * 2)
        if adapter is not GENERIC_WEB:
            db.mark_ok(conn, adapter.id)
        return article.to_dict()
    except AdapterError as exc:
        if adapter is not GENERIC_WEB:
            db.mark_error(conn, adapter.id, exc.message)
        return {"error": exc.code, "message": exc.message, "url": url, "source": adapter.id}
    except asyncio.TimeoutError:
        return {"error": "timeout", "message": "fetch timed out", "url": url, "source": adapter.id}


@mcp.tool()
def msz_search(query: str, year: int | None = None, doc_type: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Full-text search over indexed Banská Bystrica city council (MsZ) documents.

    Args:
        query: words (prefix-matched, diacritics-insensitive) or "exact phrase" in quotes.
        year: session year filter (2018+).
        doc_type: one of zapisnica, uznesenia, hlasovanie, material, pozvanka, ine.
        limit: max hits (1-50). Each hit is one page of one document with a snippet.
    """
    if doc_type and doc_type not in index.DOC_TYPES:
        return {"error": "bad_doc_type", "message": f"doc_type must be one of {', '.join(index.DOC_TYPES)}"}
    hits = index.search(conn, query, year=year, doc_type=doc_type, limit=limit, base_url=_egov_base())
    return {"query": query, "hits": hits, "index": index.stats(conn)}


@mcp.tool()
def msz_document(doc_id: int, page_from: int | None = None, page_to: int | None = None, max_chars: int = 20000) -> dict[str, Any]:
    """Return the text of an indexed MsZ document (by doc_id from msz_search/msz_sessions), optionally a page range."""
    doc = index.get_document(conn, doc_id, page_from=page_from, page_to=page_to, max_chars=max_chars, base_url=_egov_base())
    if doc is None:
        return {"error": "not_found", "message": f"no document with id {doc_id}"}
    return doc


@mcp.tool()
def msz_sessions(year: int | None = None) -> dict[str, Any]:
    """List indexed MsZ sessions (newest first) with their documents and doc_ids."""
    return {"sessions": index.list_sessions(conn, year, base_url=_egov_base()), "index": index.stats(conn)}


# --- ASGI app with token auth ----------------------------------------------


class TokenAuth:
    """Accepts /mcp/<token>[/...] (secret path, for clients without custom headers) or
    /mcp with `Authorization: Bearer <token>`. Everything else is 404. /healthz is open."""

    def __init__(self, app, token: str, mcp_path: str = "/mcp"):
        self.app = app
        self.token = token
        self.mcp_path = mcp_path.rstrip("/")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path == "/healthz":
            await self._respond(send, 200, {"ok": True, "service": "zdroje-mcp"})
            return
        if not self.token:
            await self._respond(send, 503, {"error": "MCP_TOKEN not configured"})
            return

        authorized = False
        rest = ""
        prefix = self.mcp_path + "/"
        if path.startswith(prefix):
            candidate, _, rest = path[len(prefix):].partition("/")
            if candidate and secrets.compare_digest(candidate, self.token):
                authorized = True
            elif not candidate:
                rest = ""
                authorized = self._bearer_ok(scope)
        elif path == self.mcp_path:
            authorized = self._bearer_ok(scope)

        if not authorized:
            await self._respond(send, 404, {"error": "not found"})
            return

        new_path = self.mcp_path + ("/" + rest if rest else "")
        scope = dict(scope)
        scope["path"] = new_path
        scope["raw_path"] = new_path.encode()
        await self.app(scope, receive, send)

    def _bearer_ok(self, scope) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                kind, _, tok = value.decode("latin-1").partition(" ")
                return kind.lower() == "bearer" and bool(tok) and secrets.compare_digest(tok.strip(), self.token)
        return False

    @staticmethod
    async def _respond(send, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


mcp.settings.streamable_http_path = "/mcp"
app = TokenAuth(mcp.streamable_http_app(), settings.mcp_token, "/mcp")

if not settings.mcp_token:
    log.warning("MCP_TOKEN is empty: every MCP request will be refused with 503")
