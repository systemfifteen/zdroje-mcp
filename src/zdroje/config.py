"""Runtime settings loaded from environment variables (and optional .env for local dev)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no interpolation. Existing env wins."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    mcp_token: str
    data_dir: Path
    sources_file: Path
    user_agent: str
    dennikn_cookie: str | None
    rate_limit_per_host: float
    request_timeout: float
    log_level: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "zdroje.sqlite"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "msz" / "raw"


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    contact = os.environ.get("USER_AGENT_CONTACT", "").strip()
    ua = os.environ.get("USER_AGENT", "").strip()
    if not ua:
        # Browser-like prefix keeps WAFs calm; the product token keeps us honest.
        suffix = f" zdroje-mcp/0.1 (+{contact})" if contact else " zdroje-mcp/0.1"
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0 Safari/537.36" + suffix
        )

    cookie = os.environ.get("DENNIKN_COOKIE", "").strip() or None

    return Settings(
        mcp_token=os.environ.get("MCP_TOKEN", "").strip(),
        data_dir=data_dir,
        sources_file=Path(os.environ.get("SOURCES_FILE", ROOT / "sources.yaml")),
        user_agent=ua,
        dennikn_cookie=cookie,
        rate_limit_per_host=float(os.environ.get("RATE_LIMIT_PER_HOST", "1.0")),
        request_timeout=float(os.environ.get("REQUEST_TIMEOUT", "15")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
