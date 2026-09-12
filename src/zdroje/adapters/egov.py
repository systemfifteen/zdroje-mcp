"""CG eGOV portal (egov.banskabystrica.sk): list council sessions and their documents.

The portal is ASP.NET WebForms. The session grid for one body is at
Default.aspx?NavigationState=261:0: and the year filter is a <select> that
triggers __doPostBack. Documents are served by FileOutputHttpHandler.ashx with
an opaque `arguments` token; the <img title> next to each link carries the
human-readable name, publication date and file name, which we use as the
stable identity of a document.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from .base import Adapter, AdapterError, Article, SearchResult

log = logging.getLogger(__name__)

YEAR_SELECT = "Portal1$part381$CF_Cbo_144112001"
BODY_SELECT = "Portal1$part381$CF_Cbo_144112005"
COLUMN_TYPES = {4: "pozvanka", 5: "zapisnica", 6: "uznesenia", 7: "ine"}

_TITLE_RE = re.compile(
    r'Dokument\s+"(?P<label>[^"]*)"\.\s*Dátum zverejnenia\s+(?P<pub>\d{1,2}\.\d{1,2}\.\d{4})\.'
    r'.*?súbor\s+"(?P<file>[^"]*)"(?:\s+o veľkosti\s+(?P<size>[^.]*?(?:\s?[kMG]?B)))?',
    re.S,
)
_SK_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def sk_to_iso(text: str | None) -> str | None:
    if not text:
        return None
    m = _SK_DATE.search(text)
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def classify(column: int, label: str, filename: str) -> str:
    base = COLUMN_TYPES.get(column, "ine")
    if base != "ine":
        return base
    probe = f"{label} {filename}".lower()
    if "hlasovan" in probe:
        return "hlasovanie"
    if "materi" in probe:
        return "material"
    if "zápisnic" in probe or "zapisnic" in probe:
        return "zapisnica"
    if "uznesen" in probe:
        return "uznesenia"
    if "pozvánk" in probe or "pozvank" in probe:
        return "pozvanka"
    return "ine"


@dataclass
class DocRef:
    doc_type: str
    label: str
    name: str
    published_at: str | None
    size_text: str | None
    arguments: str
    url: str


@dataclass
class Session:
    body: str
    year: int
    number: str
    date: str
    row_id: str
    docs: list[DocRef] = field(default_factory=list)


class EgovAdapter(Adapter):
    """Council documents. `search` is served from the local index (see server.py), not live."""

    @property
    def nav_state(self) -> str:
        return self.cfg.get("nav_state", "261:0:")

    @property
    def body_filter(self) -> str | None:
        """Expected text of the 'orgán' column (e.g. 'Mestské zastupiteľstvo'); rows of other bodies are dropped."""
        return self.cfg.get("body")

    @property
    def page_url(self) -> str:
        # F144112005=1 preselects the body filter (1 = Mestské zastupiteľstvo); without it the grid
        # also lists výbory mestských častí and commissions.
        extra = self.cfg.get("query_extra", "&F144112005=1")
        return f"{self.base_url}/Default.aspx?NavigationState={self.nav_state}{extra}"

    @property
    def can_search(self) -> bool:
        return False  # search is handled by msz_search over the local index

    async def search(self, query: str, since, until, limit) -> list[SearchResult]:
        raise AdapterError("unsupported", "Use msz_search for council documents")

    async def fetch(self, url: str, max_chars: int) -> Article:
        raise AdapterError("unsupported", "Use msz_document for council documents")

    # --- session listing ---------------------------------------------------

    async def list_sessions(self, year: int | None = None) -> list[Session]:
        resp = await self.http.get(self.page_url)
        if not resp.ok:
            raise AdapterError("http_error", f"eGOV returned HTTP {resp.status}")
        html = resp.text
        if year is not None:
            html = await self._select_year(html, year)
        sessions = self._parse_grid(html)
        if year is not None:
            sessions = [s for s in sessions if s.year == year]
        if self.body_filter:
            sessions = [s for s in sessions if s.body.strip().lower() == self.body_filter.strip().lower()]
        return sessions

    def available_years(self, html: str) -> list[int]:
        tree = HTMLParser(html)
        sel = tree.css_first(f'select[name="{YEAR_SELECT}"]')
        years = []
        if sel is not None:
            for opt in sel.css("option"):
                v = (opt.attributes.get("value") or "").strip()
                if v.isdigit():
                    years.append(int(v))
        return sorted(years)

    async def list_years(self) -> list[int]:
        resp = await self.http.get(self.page_url)
        if not resp.ok:
            raise AdapterError("http_error", f"eGOV returned HTTP {resp.status}")
        return self.available_years(resp.text)

    async def _select_year(self, html: str, year: int) -> str:
        """Replay the WebForms postback fired by the year <select>."""
        tree = HTMLParser(html)
        form = tree.css_first("form")
        if form is None:
            raise AdapterError("bad_response", "eGOV page has no form")
        data: dict[str, str] = {}
        for inp in form.css("input[type=hidden], input[type=text]"):
            name = inp.attributes.get("name")
            if name:
                data[name] = inp.attributes.get("value") or ""
        for sel in form.css("select"):
            name = sel.attributes.get("name")
            if not name:
                continue
            chosen = None
            for opt in sel.css("option"):
                if "selected" in opt.attributes:
                    chosen = opt.attributes.get("value", "")
            data[name] = chosen or ""
        data[YEAR_SELECT] = str(year)
        data["__EVENTTARGET"] = YEAR_SELECT
        data["__EVENTARGUMENT"] = ""
        action = form.attributes.get("action") or self.page_url
        post_url = urljoin(self.page_url, htmllib.unescape(action))
        resp = await self.http.post(post_url, data=data, headers={"Referer": self.page_url})
        if not resp.ok:
            raise AdapterError("http_error", f"eGOV postback returned HTTP {resp.status}")
        return resp.text

    def _parse_grid(self, html: str) -> list[Session]:
        tree = HTMLParser(html)
        sessions: list[Session] = []
        for tr in tree.css("tr[rid]"):  # attribute names are case-insensitive in HTML
            row_id = tr.attributes.get("rId") or tr.attributes.get("rid") or ""
            cells = tr.css("td")
            if len(cells) < 8:
                continue
            body = cells[0].text(strip=True)
            year_txt = cells[1].text(strip=True)
            number = cells[2].text(strip=True)
            sdate = sk_to_iso(cells[3].text(strip=True))
            if not (year_txt.isdigit() and sdate):
                continue
            session = Session(body=body, year=int(year_txt), number=number, date=sdate, row_id=row_id)
            for col in range(4, len(cells)):
                for a in cells[col].css("a[href]"):
                    href = htmllib.unescape(a.attributes.get("href", ""))
                    if "FileOutputHttpHandler" not in href:
                        continue
                    img = a.css_first("img")
                    title = htmllib.unescape(img.attributes.get("title", "")) if img is not None else ""
                    m = _TITLE_RE.search(title)
                    if m:
                        label = m.group("label").strip()
                        name = m.group("file").strip()
                        pub = sk_to_iso(m.group("pub"))
                        size = (m.group("size") or "").strip() or None
                    else:
                        label, name, pub, size = "", title or href, None, None
                        log.warning("eGOV: unparsed document title %r", title[:120])
                    args = href.split("arguments=", 1)[1] if "arguments=" in href else href
                    session.docs.append(
                        DocRef(
                            doc_type=classify(col, label, name),
                            label=label,
                            name=name,
                            published_at=pub,
                            size_text=size,
                            arguments=args,
                            url=urljoin(self.page_url, href),
                        )
                    )
            sessions.append(session)
        return sessions

    # --- downloads ---------------------------------------------------------

    async def download(self, url: str) -> tuple[bytes, str | None]:
        # Material ZIPs run to tens of MB; give them a generous read timeout.
        status, content, headers = await self.http.get_bytes(url, timeout=180.0)
        if status != 200:
            raise AdapterError("http_error", f"eGOV download returned HTTP {status}")
        return content, headers.get("content-type")
