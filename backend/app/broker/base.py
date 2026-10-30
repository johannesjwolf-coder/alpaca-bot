"""Gemeinsame Broker-Schnittstelle, Datenmodelle und Fehlerklassen.

DEMO- und PAPER-Broker implementieren dieselbe Schnittstelle. Alle
Geschäftsregeln (Risiko, Order-Validierung, Abgleich) sitzen oberhalb davon und
sind daher für beide Betriebsarten und beide Authentifizierungsarten identisch.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

STOCK_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?\Z")
# Krypto ausschliesslich gegen USD (das Paper-Konto rechnet in USD), z. B. BTC/USD.
CRYPTO_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}/USD\Z")
SYMBOL_RE = re.compile(r"^(?:[A-Z]{1,5}(?:\.[A-Z]{1,2})?|[A-Z0-9]{2,10}/USD)\Z")


def is_crypto(symbol: str) -> bool:
    return bool(CRYPTO_SYMBOL_RE.match(symbol))


def canonical_symbol(symbol: str, asset_class: str | None = None) -> str:
    """Einheitliche Schreibweise: Krypto immer mit Schrägstrich (Alpaca liefert teils BTCUSD)."""
    s = symbol.strip().upper()
    if "/" not in s and asset_class == "crypto" and s.endswith("USD") and len(s) > 3:
        return f"{s[:-3]}/USD"
    return s
UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# Alpaca-Orderstatus, die sich nicht mehr ändern.
FINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}
# Lokale Zustände (vor/neben dem Broker), Präfix "local_".
LOCAL_PENDING = "local_pending"  # gespeichert, Antwort des Brokers steht noch aus
LOCAL_UNKNOWN = "local_unknown"  # Ergebnis unklar (Timeout o. ä.) – Abgleich nötig
LOCAL_FAILED = "local_failed"  # Broker hat die Anfrage abgelehnt, keine Order entstanden
LOCAL_NOT_RECEIVED = "local_not_received"  # Abgleich: Broker kennt die Order nicht
LOCAL_FINAL = {LOCAL_FAILED, LOCAL_NOT_RECEIVED}
LOCAL_UNRESOLVED = {LOCAL_PENDING, LOCAL_UNKNOWN}


def is_open_status(status: str) -> bool:
    return status not in FINAL_ORDER_STATUSES and status not in LOCAL_FINAL


class BrokerError(Exception):
    """Klassifizierter Fehler eines Broker-Aufrufs.

    kind:
      config_missing, auth_rejected, forbidden, network, timeout, rate_limited,
      broker_error, not_found, unprocessable, invalid_url, unexpected_redirect,
      invalid_response
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        status_code: int | None = None,
        hint: str | None = None,
        may_have_been_sent: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code
        self.hint = hint
        self.may_have_been_sent = may_have_been_sent
        self.retry_after_s = retry_after_s

    @property
    def is_connectivity(self) -> bool:
        return self.kind in {"network", "timeout"}

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "status_code": self.status_code,
            "hint": self.hint,
        }


class Account(BaseModel):
    status: str
    currency: str
    cash: float
    equity: float
    last_equity: float | None = None
    buying_power: float | None = None
    non_marginable_buying_power: float | None = None
    trading_blocked: bool = False
    account_blocked: bool = False
    trade_suspended_by_user: bool = False
    multiplier: str | None = None


class Clock(BaseModel):
    timestamp: datetime
    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None


class Asset(BaseModel):
    symbol: str
    name: str = ""
    asset_class: str
    exchange: str = ""
    status: str
    tradable: bool
    fractionable: bool = False
    shortable: bool = False
    marginable: bool = False
    min_order_size: float | None = None  # Krypto: kleinste Ordermenge
    min_trade_increment: float | None = None  # Krypto: Mengenschritt


class Position(BaseModel):
    symbol: str
    qty: float
    qty_available: float
    side: str = "long"
    avg_entry_price: float = 0.0
    market_value: float = 0.0
    cost_basis: float = 0.0
    unrealized_pl: float = 0.0
    unrealized_intraday_pl: float | None = None
    current_price: float | None = None
    asset_class: str = "us_equity"


class Order(BaseModel):
    id: str
    client_order_id: str
    symbol: str
    side: str
    type: str = "market"
    time_in_force: str = "day"
    qty: float | None = None
    notional: float | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    canceled_at: datetime | None = None


class Bar(BaseModel):
    t: datetime  # Beginn der Minute, UTC
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float = 0.0


class OrderRequest(BaseModel):
    symbol: str
    side: str
    qty: Decimal | None = None
    notional: Decimal | None = None
    time_in_force: str = "day"
    client_order_id: str
    type: str = "market"


class LatestPrice(BaseModel):
    price: float
    at: datetime
    source: str


class Quote(BaseModel):
    """Bester Geld-/Briefkurs. Käufe zahlen ``ask``, Verkäufe erhalten ``bid``."""

    symbol: str
    bid: float
    ask: float
    at: datetime
    source: str

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid > 0 else float("inf")


class Broker(ABC):
    """Gemeinsame Schnittstelle für DEMO- und PAPER-Broker."""

    mode: str
    data_source_label: str
    data_feed: str
    # True: echte Börsensitzungen (Strategie nutzt nur Kerzen der laufenden Sitzung).
    uses_sessions: bool = False

    @abstractmethod
    async def get_account(self) -> Account: ...

    @abstractmethod
    async def get_clock(self) -> Clock: ...

    @abstractmethod
    async def get_asset(self, symbol: str) -> Asset: ...

    @abstractmethod
    async def list_positions(self) -> list[Position]: ...

    @abstractmethod
    async def list_orders(
        self, status: str = "open", limit: int = 100, after: datetime | None = None
    ) -> list[Order]: ...

    @abstractmethod
    async def get_order(self, order_id: str) -> Order: ...

    @abstractmethod
    async def get_order_by_client_id(self, client_order_id: str) -> Order | None: ...

    @abstractmethod
    async def submit_order(self, req: OrderRequest) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    async def get_bars(self, symbol: str, limit: int) -> list[Bar]: ...

    @abstractmethod
    async def get_latest_price(self, symbol: str) -> LatestPrice | None: ...

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote | None: ...

    async def get_session_open(self, now: datetime) -> datetime | None:
        """Beginn der regulären Handelssitzung am Handelstag von ``now`` (UTC) oder None."""
        return None

    async def aclose(self) -> None:  # pragma: no cover - optional
        return None
