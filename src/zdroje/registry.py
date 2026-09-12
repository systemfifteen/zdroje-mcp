"""Load sources.yaml and instantiate adapters."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import yaml

from .adapters import ADAPTERS, Adapter, EgovAdapter
from .config import Settings
from .http import Http

log = logging.getLogger(__name__)


class Registry:
    def __init__(self, settings: Settings, conn: sqlite3.Connection):
        self.settings = settings
        self.conn = conn
        self.http = Http(settings, conn)
        self.adapters: dict[str, Adapter] = {}
        self._load(settings.sources_file)

    def _load(self, path: Path) -> None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for cfg in raw:
            if cfg.get("enabled", True) is False:
                log.info("source %s disabled", cfg.get("id"))
                continue
            cls = ADAPTERS.get(cfg.get("adapter"))
            if cls is None:
                log.error("source %s: unknown adapter %r", cfg.get("id"), cfg.get("adapter"))
                continue
            adapter = cls(cfg, self.http, self.settings)
            self.adapters[adapter.id] = adapter
        log.info("loaded sources: %s", ", ".join(self.adapters))

    @property
    def news(self) -> list[Adapter]:
        return [a for a in self.adapters.values() if a.can_search]

    @property
    def egov(self) -> EgovAdapter | None:
        for a in self.adapters.values():
            if isinstance(a, EgovAdapter):
                return a
        return None

    def for_url(self, url: str) -> Adapter | None:
        for a in self.adapters.values():
            if a.matches(url):
                return a
        return None

    async def aclose(self) -> None:
        await self.http.aclose()
