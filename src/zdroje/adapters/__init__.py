from .base import Adapter, AdapterError, Article, SearchResult
from .dennikn import DennikNAdapter
from .egov import EgovAdapter
from .html_search import HtmlSearchAdapter
from .wordpress_api import WordPressApiAdapter
from .wordpress_rss import WordPressRssAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "wordpress_api": WordPressApiAdapter,
    "wordpress_rss": WordPressRssAdapter,
    "html_search": HtmlSearchAdapter,
    "dennikn": DennikNAdapter,
    "egov": EgovAdapter,
}

__all__ = [
    "ADAPTERS",
    "Adapter",
    "AdapterError",
    "Article",
    "SearchResult",
    "DennikNAdapter",
    "EgovAdapter",
    "HtmlSearchAdapter",
    "WordPressApiAdapter",
    "WordPressRssAdapter",
]
