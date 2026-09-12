"""Offline unit tests: eGOV grid parsing, FTS query building, token auth, Slovak date parsing."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from zdroje import db, index
from zdroje.adapters.egov import EgovAdapter, classify, sk_to_iso
from zdroje.adapters.html_search import parse_sk_date
from zdroje.config import Settings
from zdroje.server import TokenAuth

FIXTURES = Path(__file__).parent / "fixtures"

# One real row from egov.banskabystrica.sk (2026-09-08), attributes as served (&quot; escaped titles).
EGOV_ROW_HTML = """
<html><body><form action="./Default.aspx?NavigationState=261:0:&amp;F144112005=1" method="post">
<input type="hidden" name="__VIEWSTATE" value="vs"/><input type="hidden" name="__EVENTVALIDATION" value="ev"/>
<select name="Portal1$part381$CF_Cbo_144112001"><option value="">Všetky roky</option><option selected="selected" value="2026">2026</option><option value="2018">2018</option></select>
<select name="Portal1$part381$CF_Cbo_144112005"><option selected="selected" value="1">Mestské zastupiteľstvo</option></select>
<table><tbody>
<tr rId="1514">
 <td>Mestské zastupiteľstvo</td><td>2026</td><td>24</td><td>08.09.2026</td>
 <td><a class="grdLink" href="FileOutputHttpHandler.ashx?arguments=AAA%3d%3d"><img alt="Dokument_1" title="Dokument &quot;Pozvánka&quot;. Dátum zverejnenia 1.9.2026. Po kliknutí na túto ikonu sa otvorí súbor &quot;POZVÁNKA MSZ 8.9.2026..PDF&quot; o veľkosti 324,2 kB."/></a></td>
 <td></td>
 <td></td>
 <td><a class="grdLink" href="FileOutputHttpHandler.ashx?arguments=BBB"><img title="Dokument &quot;Doplnený materiál&quot;. Dátum zverejnenia 11.9.2026. Po kliknutí na túto ikonu sa otvorí súbor &quot;DOPLNENÝ MATERIÁL MSZ 8.9.2026.ZIP&quot; o veľkosti 739,6 kB."/></a><a class="grdLink" href="FileOutputHttpHandler.ashx?arguments=CCC"><img title="Dokument &quot;Hlasovanie&quot;. Dátum zverejnenia 9.9.2026. Po kliknutí na túto ikonu sa otvorí súbor &quot;HLASOVANIE MSZ BB 8.9.2026.PDF&quot; o veľkosti 668,4 kB."/></a></td>
</tr>
<tr rId="1500">
 <td>Výbor mestskej časti</td><td>2026</td><td>3</td><td>21.09.2026</td><td></td><td></td><td></td><td></td>
</tr>
</tbody></table></form></body></html>
"""


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        mcp_token="t", data_dir=tmp_path, sources_file=tmp_path / "s.yaml", user_agent="test",
        dennikn_cookie=None, rate_limit_per_host=0, request_timeout=5, log_level="WARNING",
    )


def test_egov_grid_parsing(tmp_path):
    adapter = EgovAdapter({"id": "msz", "base_url": "https://egov.banskabystrica.sk", "body": "Mestské zastupiteľstvo"}, http=None, settings=_settings(tmp_path))
    sessions = adapter._parse_grid(EGOV_ROW_HTML)
    assert len(sessions) == 2  # body filter is applied in list_sessions, not in the parser
    s = sessions[0]
    assert (s.body, s.year, s.number, s.date, s.row_id) == ("Mestské zastupiteľstvo", 2026, "24", "2026-09-08", "1514")
    assert [d.doc_type for d in s.docs] == ["pozvanka", "material", "hlasovanie"]
    pozvanka = s.docs[0]
    assert pozvanka.name == "POZVÁNKA MSZ 8.9.2026..PDF"
    assert pozvanka.published_at == "2026-09-01"
    assert pozvanka.size_text == "324,2 kB"
    assert pozvanka.arguments == "AAA%3d%3d"
    assert pozvanka.url == "https://egov.banskabystrica.sk/FileOutputHttpHandler.ashx?arguments=AAA%3d%3d"
    assert adapter.available_years(EGOV_ROW_HTML) == [2018, 2026]


def test_classify_and_dates():
    assert classify(7, "Hlasovanie", "HLASOVANIE MSZ BB 8.9.2026.PDF") == "hlasovanie"
    assert classify(7, "Doplnený materiál", "X.ZIP") == "material"
    assert classify(7, "Účasť poslancov na MsZ", "UCAST.PDF") == "ine"
    assert classify(5, "whatever", "x.docx") == "zapisnica"
    assert sk_to_iso("08.09.2026") == "2026-09-08"
    assert sk_to_iso("31.02.2026") is None
    assert parse_sk_date("8. septembra 2026") == "2026-09-08"
    assert parse_sk_date("2026-09-08T16:39:28+02:00") == "2026-09-08T16:39:28+02:00"
    assert parse_sk_date("15:59") is None


def test_fts_query_building():
    assert index.fts_query("Skubín predaj domu") == '"Skubín"* "predaj"* "domu"*'
    assert index.fts_query('"mestský dom" 2026') == '"mestský dom" "2026"'
    assert index.fts_query("a b") == '"a" "b"'
    assert index.fts_query("!!!") == '""'


def test_index_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite")
    conn.execute("INSERT INTO sessions(body, year, number, date) VALUES ('MsZ', 2026, '24', '2026-09-08')")
    conn.execute("INSERT INTO documents(session_id, doc_type, name, egov_arguments, pages, indexed_at) VALUES (1, 'zapisnica', 'Z.PDF', 'ARG', 2, 'now')")
    conn.execute("INSERT INTO pages(document_id, page_no, text) VALUES (1, 1, 'Budova bývalej ZŠ Skubín je v zlom stave.')")
    conn.execute("INSERT INTO pages(document_id, page_no, text) VALUES (1, 2, 'Hlasovanie o parkovaní.')")
    conn.commit()
    hits = index.search(conn, "skubin", base_url="https://egov.example")  # no diacritics in query
    assert len(hits) == 1 and hits[0]["page"] == 1 and "<b>Skubín</b>" in hits[0]["snippet"]
    assert hits[0]["source_url"] == "https://egov.example/FileOutputHttpHandler.ashx?arguments=ARG"
    assert index.search(conn, "skubin", doc_type="hlasovanie") == []
    doc = index.get_document(conn, 1, page_from=2, base_url="")
    assert doc["total_pages"] == 2 and doc["page_from"] == 2 and "parkovaní" in doc["text"]
    assert index.get_document(conn, 99) is None
    assert index.stats(conn)["pages"] == 2


async def _call(app, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> tuple[int, str]:
    status, chunks = 0, []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        nonlocal status
        if msg["type"] == "http.response.start":
            status = msg["status"]
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    await app({"type": "http", "path": path, "headers": headers or [], "method": "POST"}, receive, send)
    return status, b"".join(chunks).decode()


def test_token_auth_paths():
    seen_paths = []

    async def inner(scope, receive, send):
        seen_paths.append(scope["path"])
        await TokenAuth._respond(send, 200, {"inner": True})

    app = TokenAuth(inner, "s3cret", "/mcp")
    run = lambda *a: asyncio.run(_call(app, *a))  # noqa: E731

    assert run("/healthz")[0] == 200
    assert run("/mcp")[0] == 404
    assert run("/mcp/wrong")[0] == 404
    assert run("/mcp/s3cret")[0] == 200
    assert run("/mcp", [(b"authorization", b"Bearer s3cret")])[0] == 200
    assert run("/mcp", [(b"authorization", b"Bearer nope")])[0] == 404
    assert run("/mcp/", [(b"authorization", b"Bearer s3cret")])[0] == 200
    assert run("/other")[0] == 404
    assert seen_paths == ["/mcp", "/mcp", "/mcp"]  # token stripped, inner app always sees /mcp

    assert asyncio.run(_call(TokenAuth(inner, "", "/mcp"), "/mcp/x"))[0] == 503
