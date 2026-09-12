"""Article text extraction: trafilatura with optional per-source CSS scoping."""

from __future__ import annotations

import html as htmllib
import logging
import re
from dataclasses import dataclass

import trafilatura
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)

_WS = re.compile(r"[ \t ]+")
_TAGS = re.compile(r"<[^>]+>")


def strip_tags(fragment: str | None) -> str:
    if not fragment:
        return ""
    text = _TAGS.sub(" ", fragment)
    text = htmllib.unescape(text)
    return _WS.sub(" ", text).strip()


@dataclass
class Extracted:
    title: str | None
    author: str | None
    date: str | None
    text: str


def _scope_html(html: str, selector: str | None) -> str:
    """Return only the part of the page matched by `selector` (first match), else the whole page."""
    if not selector:
        return html
    try:
        tree = HTMLParser(html)
        node = tree.css_first(selector)
        if node is not None:
            return f"<html><body>{node.html}</body></html>"
    except Exception as exc:  # selector syntax errors etc. must not break extraction
        log.warning("selector %r failed: %s", selector, exc)
    return html


def extract_article(html: str, url: str, *, body_selector: str | None = None) -> Extracted:
    meta = None
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
    except Exception as exc:
        log.debug("metadata extraction failed for %s: %s", url, exc)

    scoped = _scope_html(html, body_selector)
    text = trafilatura.extract(
        scoped,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=False,
        include_comments=False,
        favor_recall=True,
    )
    if not text and scoped is not html:
        text = trafilatura.extract(
            html, url=url, output_format="markdown", include_tables=True, include_links=False
        )

    return Extracted(
        title=getattr(meta, "title", None),
        author=getattr(meta, "author", None),
        date=getattr(meta, "date", None),
        text=(text or "").strip(),
    )


def html_fragment_to_markdown(fragment: str, url: str) -> str:
    """Convert an HTML fragment (e.g. WordPress content.rendered) to Markdown."""
    wrapped = f"<html><body><article>{fragment}</article></body></html>"
    text = trafilatura.extract(
        wrapped, url=url, output_format="markdown", include_tables=True, include_links=False,
        favor_recall=True,
    )
    return (text or strip_tags(fragment)).strip()
