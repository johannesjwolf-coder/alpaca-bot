"""Laufzeitkonfiguration aus Umgebungsvariablen.

Zugangsdaten werden hier bewusst NICHT gelesen. Nur der Broker-Adapter liest
ALPACA_API_KEY/ALPACA_SECRET_KEY im Modus ``env`` direkt vor der Anfrage.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent

# Feste Zieladressen. Es gibt absichtlich keine Einstellung, um sie zu ändern.
PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
MARKET_DATA_BASE_URL = "https://data.alpaca.markets"
PAPER_TRADING_HOST = "paper-api.alpaca.markets"
MARKET_DATA_HOST = "data.alpaca.markets"

AUTH_MODES = ("proxy", "env")
APP_MODES = ("DEMO", "PAPER")


def load_dotenv(path: Path) -> None:
    """Minimaler .env-Leser (nur KEY=VALUE), überschreibt keine gesetzten Variablen."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: Path = field(default_factory=lambda: BACKEND_DIR / "data" / "alpaca-bot.sqlite3")
    start_mode: str = "DEMO"
    allowed_hosts: list[str] = field(default_factory=lambda: ["localhost", "127.0.0.1"])
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )
    allow_nonlocal_bind: bool = False
    frontend_dist: Path = field(default_factory=lambda: REPO_DIR / "frontend" / "dist")
    sync_interval_s: float = 10.0
    bot_interval_s: float = 15.0

    @property
    def binds_loopback(self) -> bool:
        return _is_loopback(self.host)


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def load_settings() -> Settings:
    load_dotenv(REPO_DIR / ".env")
    load_dotenv(BACKEND_DIR / ".env")
    s = Settings()
    s.host = os.environ.get("APP_HOST", s.host)
    s.port = int(os.environ.get("APP_PORT", s.port))
    if os.environ.get("APP_DB_PATH"):
        s.db_path = Path(os.environ["APP_DB_PATH"])
    start_mode = os.environ.get("APP_START_MODE", "DEMO").strip().upper()
    s.start_mode = start_mode if start_mode in APP_MODES else "DEMO"
    if os.environ.get("APP_ALLOWED_HOSTS"):
        s.allowed_hosts = _split(os.environ["APP_ALLOWED_HOSTS"])
    if os.environ.get("APP_ALLOWED_ORIGINS"):
        s.allowed_origins = _split(os.environ["APP_ALLOWED_ORIGINS"])
    s.allow_nonlocal_bind = os.environ.get("APP_ALLOW_NONLOCAL_BIND", "") == "1"
    if os.environ.get("APP_FRONTEND_DIST"):
        s.frontend_dist = Path(os.environ["APP_FRONTEND_DIST"])
    return s


def auth_mode_from_env() -> str | None:
    value = os.environ.get("ALPACA_AUTH_MODE", "").strip().lower()
    return value if value in AUTH_MODES else None
