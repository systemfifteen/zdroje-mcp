# zdroje-mcp

Remote [MCP](https://modelcontextprotocol.io) server that gives Claude structured access to
Banská Bystrica news sources (BBonline, Bystricoviny, Bystricak, Denník N) and to the city
council (MsZ) documents published on the CG eGOV portal: zápisnice, uznesenia, hlasovania, materiály.

Single-user, personal research tool. Product requirements live in [PRD.md](PRD.md).

## Tools

| Tool | What it does |
|---|---|
| `list_sources` | registry, capabilities, last error per source, index statistics |
| `search(query, sources?, since?, until?, limit?)` | parallel search across news sources, merged newest-first; add `msz` to include council docs |
| `fetch(url, max_chars?)` | article text as Markdown; Denník N uses the owner's subscription cookie |
| `msz_search(query, year?, doc_type?, limit?)` | FTS5 full-text search over indexed MsZ documents (page-level hits with snippets) |
| `msz_document(doc_id, page_from?, page_to?, max_chars?)` | text of one indexed document |
| `msz_sessions(year?)` | sessions with their documents and `doc_id`s |

## Run locally

```bash
uv sync
cp .env.example .env          # set MCP_TOKEN (and DENNIKN_COOKIE if you have a subscription)
uv run uvicorn zdroje.server:app --port 8000
uv run python -m zdroje.indexer --year 2026   # build the MsZ index (needs pdftotext or pypdf)
```

Client URL: `http://localhost:8000/mcp/<MCP_TOKEN>` or `http://localhost:8000/mcp` with
`Authorization: Bearer <MCP_TOKEN>`.

## Deploy (Coolify)

Dockerfile app, port 8000, persistent volume mounted at `/data`. Environment:

| Variable | Purpose |
|---|---|
| `MCP_TOKEN` | required; long random secret (`python -c "import secrets; print(secrets.token_urlsafe(32))"`) |
| `DENNIKN_COOKIE` | optional; `Cookie` header value of a logged-in dennikn.sk session |
| `USER_AGENT_CONTACT` | optional; email/URL appended to the User-Agent |
| `DISABLE_CRON` | set `1` to skip the nightly indexer |

The container runs the nightly indexer (03:10 Europe/Bratislava) via supercronic. First backfill:

```bash
docker exec -it <container> python -m zdroje.indexer --all
```

## Adding a source

Append an entry to `sources.yaml`. WordPress sites with the REST API need only `adapter: wordpress_api`
and `base_url`; sites with the API disabled use `wordpress_rss`; anything else uses `html_search`
with CSS selectors for the result list.

## Conduct

Max one request per second per host, cached responses, honest User-Agent. Paywalled content is
fetched only with the owner's own subscription and never redistributed.
