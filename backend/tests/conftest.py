"""Test-Hilfen: zustandsbehafteter Fake-Alpaca-Server (kein Netzwerk), feste Uhr, Kontext."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.broker.alpaca import AlpacaPaperBroker, EnvAuth, ProxyAuth
from app.config import Settings
from app.core import AppContext
from app.db import Database

# Freitag, 30.10.2026, 14:30 UTC = 10:30 New York (Markt offen)
T0 = datetime(2026, 10, 30, 14, 30, 5, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t = self.t + timedelta(**kw)


class FakeAlpaca:
    """Simuliert die benötigten Endpunkte von paper-api und data.alpaca.markets."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.account = {
            "status": "ACTIVE",
            "currency": "USD",
            "cash": "1000",
            "equity": "1000",
            "last_equity": "1000",
            "buying_power": "4000",
            "non_marginable_buying_power": "1000",
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
            "multiplier": "4",
            "account_number": "PA-GEHEIM-123",
        }
        self.positions: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.is_open = True
        self.assets = {
            "SPY": {"symbol": "SPY", "name": "SPDR S&P 500", "class": "us_equity", "exchange": "ARCA",
                    "status": "active", "tradable": True, "fractionable": True},
            "BRK.A": {"symbol": "BRK.A", "name": "Berkshire A", "class": "us_equity", "exchange": "NYSE",
                      "status": "active", "tradable": True, "fractionable": False},
            "OTCX": {"symbol": "OTCX", "name": "OTC Ding", "class": "us_equity", "exchange": "OTC",
                     "status": "active", "tradable": True, "fractionable": False},
            "DEAD": {"symbol": "DEAD", "name": "Inaktiv", "class": "us_equity", "exchange": "NYSE",
                     "status": "inactive", "tradable": False, "fractionable": False},
        }
        self.prices = {"SPY": 10.0, "BRK.A": 600000.0}
        self.bars: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str, dict]] = []
        # Fehler-Injektion: Schlüssel (METHODE, Pfad) -> Liste von Aktionen (pro Aufruf eine)
        self.inject: dict[tuple[str, str], list] = {}

    # ----------------------------------------------------------- helpers
    def add_position(self, symbol: str, qty: float, price: float, avg: float | None = None) -> None:
        self.positions.append(
            {
                "symbol": symbol, "qty": str(qty), "qty_available": str(qty), "side": "long",
                "avg_entry_price": str(avg or price), "market_value": str(qty * price),
                "cost_basis": str(qty * (avg or price)), "unrealized_pl": str(qty * (price - (avg or price))),
                "current_price": str(price), "asset_class": "us_equity",
            }
        )

    def add_order(self, **kw) -> dict:
        oid = str(uuid.uuid4())
        o = {
            "id": oid, "client_order_id": kw.get("client_order_id", f"ext-{oid[:8]}"), "symbol": kw["symbol"],
            "side": kw["side"], "type": "market", "time_in_force": kw.get("time_in_force", "day"),
            "qty": kw.get("qty"), "notional": kw.get("notional"), "filled_qty": kw.get("filled_qty", "0"),
            "filled_avg_price": kw.get("filled_avg_price"), "status": kw.get("status", "new"),
            "created_at": self.clock().isoformat().replace("+00:00", "Z"),
            "updated_at": self.clock().isoformat().replace("+00:00", "Z"),
        }
        self.orders[oid] = o
        return o

    def make_bars(self, symbol: str, closes: list[float], end_minute: datetime | None = None) -> None:
        """Kerzen, deren letzte bei ``end_minute`` beginnt (Standard: letzte abgeschlossene Minute)."""
        end = end_minute or (self.clock().replace(second=0, microsecond=0) - timedelta(minutes=1))
        start = end - timedelta(minutes=len(closes) - 1)
        self.bars[symbol] = [
            {"t": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
             "o": c, "h": c, "l": c, "c": c, "v": 100}
            for i, c in enumerate(closes)
        ]

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p == path)

    # ----------------------------------------------------------- handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append((method, path, dict(request.headers)))
        actions = self.inject.get((method, path))
        if actions:
            action = actions.pop(0)
            if isinstance(action, Exception):
                raise action
            if isinstance(action, httpx.Response):
                return action
            if callable(action):
                result = action(request)
                if result is not None:
                    return result
        host = request.url.host
        q = parse_qs(request.url.query.decode())
        if host == "data.alpaca.markets":
            sym = path.split("/")[3]
            if path.endswith("/bars"):
                bars = list(self.bars.get(sym, []))
                if q.get("sort") == ["desc"]:
                    bars = list(reversed(bars))
                return httpx.Response(200, json={"bars": bars, "symbol": sym, "next_page_token": None})
            if path.endswith("/trades/latest"):
                if sym not in self.prices:
                    return httpx.Response(404, json={"message": "not found"})
                return httpx.Response(200, json={"symbol": sym, "trade": {"t": self.clock().isoformat(), "p": self.prices[sym]}})
        if path == "/v2/account":
            return httpx.Response(200, json=self.account)
        if path == "/v2/clock":
            return httpx.Response(200, json={"timestamp": self.clock().isoformat(), "is_open": self.is_open,
                                             "next_open": "2026-11-02T14:30:00Z", "next_close": "2026-10-30T20:00:00Z"})
        if path == "/v2/positions":
            return httpx.Response(200, json=self.positions)
        if path.startswith("/v2/assets/"):
            sym = path.rsplit("/", 1)[1]
            a = self.assets.get(sym)
            return httpx.Response(200, json=a) if a else httpx.Response(404, json={"message": "asset not found"})
        if path == "/v2/orders" and method == "GET":
            status = q.get("status", ["open"])[0]
            items = list(self.orders.values())
            if status == "open":
                items = [o for o in items if o["status"] in ("new", "accepted", "partially_filled", "pending_new")]
            return httpx.Response(200, json=items)
        if path == "/v2/orders:by_client_order_id":
            cid = q["client_order_id"][0]
            for o in self.orders.values():
                if o["client_order_id"] == cid:
                    return httpx.Response(200, json=o)
            return httpx.Response(404, json={"message": "order not found"})
        if path == "/v2/orders" and method == "POST":
            body = json.loads(request.content)
            if any(o["client_order_id"] == body["client_order_id"] for o in self.orders.values()):
                return httpx.Response(422, json={"message": "client_order_id must be unique"})
            o = self.add_order(**{k: body.get(k) for k in ("symbol", "side", "qty", "notional", "time_in_force", "client_order_id")},
                               status="accepted")
            return httpx.Response(200, json=o)
        if path.startswith("/v2/orders/") and method == "GET":
            o = self.orders.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=o) if o else httpx.Response(404, json={"message": "not found"})
        if path.startswith("/v2/orders/") and method == "DELETE":
            o = self.orders.get(path.rsplit("/", 1)[1])
            if not o:
                return httpx.Response(404, json={"message": "not found"})
            o["status"] = "canceled"
            return httpx.Response(204)
        return httpx.Response(500, json={"message": f"unhandled {method} {path}"})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake(clock: FakeClock) -> FakeAlpaca:
    return FakeAlpaca(clock)


@pytest.fixture
def env_keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "PKTESTKEYID")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "testsecretvalue")


@pytest.fixture
def paper_broker(fake: FakeAlpaca, env_keys) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(EnvAuth(), transport=httpx.MockTransport(fake.handler), backoff_base_s=0.001)


@pytest.fixture
def proxy_broker(fake: FakeAlpaca, monkeypatch) -> AlpacaPaperBroker:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    return AlpacaPaperBroker(ProxyAuth(), transport=httpx.MockTransport(fake.handler), backoff_base_s=0.001)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.sqlite3",
        allowed_hosts=["testserver", "localhost", "127.0.0.1"],
        frontend_dist=tmp_path / "no-dist",
        bot_interval_s=3600,
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
async def ctx(settings, db, paper_broker, clock) -> AppContext:
    c = AppContext(settings, db, paper_broker=paper_broker, now=clock)
    await c.startup(background=False)
    yield c
    await c.shutdown()


@pytest.fixture
async def paper_ctx(ctx: AppContext) -> AppContext:
    await ctx.set_mode("PAPER")
    ctx.set_orders_enabled("PAPER", True)
    return ctx
