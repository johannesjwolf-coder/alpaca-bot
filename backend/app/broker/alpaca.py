"""REST-Adapter für Alpaca Paper Trading und Alpaca Market Data.

Sicherheitsregeln:
* Handelsanfragen gehen ausschliesslich an https://paper-api.alpaca.markets,
  Marktdaten ausschliesslich an https://data.alpaca.markets. Die URLs sind fest
  und werden vor jeder Anfrage (und zusätzlich per Request-Hook) geprüft.
* Redirects werden nicht verfolgt, sondern als Fehler behandelt.
* Proxy- und Zertifikatskonfiguration der Umgebung bleiben erhalten
  (trust_env=True, TLS-Prüfung aktiv).
* Schreibende Anfragen (POST/DELETE) werden nie automatisch wiederholt.
* Zugangsdaten werden nie geloggt oder in Fehlermeldungen übernommen.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from ..config import (
    MARKET_DATA_BASE_URL,
    MARKET_DATA_HOST,
    PAPER_TRADING_BASE_URL,
    PAPER_TRADING_HOST,
    auth_mode_from_env,
)
from ..timeutil import NEW_YORK
from .base import (
    CRYPTO_SYMBOL_RE,
    STOCK_SYMBOL_RE,
    UUID_RE,
    Account,
    Asset,
    Bar,
    Broker,
    BrokerError,
    Clock,
    LatestPrice,
    Order,
    OrderRequest,
    Position,
    TIMEFRAMES,
    Quote,
    canonical_symbol,
    is_crypto,
)

log = logging.getLogger("alpaca_bot.broker")

BASES = {"trading": PAPER_TRADING_BASE_URL, "data": MARKET_DATA_BASE_URL}
HOSTS = {"trading": PAPER_TRADING_HOST, "data": MARKET_DATA_HOST}
ALLOWED_HOSTS = frozenset(HOSTS.values())
DATA_FEED = "iex"
CRYPTO_LOCATION = "us"
# Rückblick je Intervall für "jüngste Kerzen" (deckt Wochenenden/Feiertage ab).
LOOKBACK = {
    "1Min": timedelta(days=5),
    "5Min": timedelta(days=20),
    "15Min": timedelta(days=45),
    "1Hour": timedelta(days=120),
    "1Day": timedelta(days=1000),
}
MAX_PAGES = 10
# Erlaubte API-Pfade je Host. Krypto-Marktdaten liegen bei Alpaca unter /v1beta3.
PATH_PREFIXES = {"trading": ("/v2/",), "data": ("/v2/stocks/", f"/v1beta3/crypto/{CRYPTO_LOCATION}/")}

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
READ_ATTEMPTS = 3


# --------------------------------------------------------------------------- #
# Authentifizierung
# --------------------------------------------------------------------------- #
class AuthStrategy:
    mode: str = "unset"

    def headers(self) -> dict[str, str]:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError


class ProxyAuth(AuthStrategy):
    """Die Cloud-Umgebung ergänzt APCA-API-KEY-ID/APCA-API-SECRET-KEY per Network Secret."""

    mode = "proxy"

    def headers(self) -> dict[str, str]:
        if not (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")):
            raise BrokerError(
                "config_missing",
                "ALPACA_AUTH_MODE=proxy, aber es ist kein HTTPS-Proxy konfiguriert.",
                hint=(
                    "Der Proxy-Modus funktioniert nur in Claude Code im Web mit einem "
                    "Network Secret. Auf dem eigenen PC/Server ALPACA_AUTH_MODE=env verwenden."
                ),
            )
        return {}

    def describe(self) -> dict[str, Any]:
        return {
            "auth_mode": "proxy",
            "https_proxy_configured": bool(
                os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            ),
            "ca_bundle_configured": bool(os.environ.get("SSL_CERT_FILE")),
        }


class EnvAuth(AuthStrategy):
    """Liest ALPACA_API_KEY/ALPACA_SECRET_KEY aus der Laufzeitumgebung des Backends."""

    mode = "env"

    def headers(self) -> dict[str, str]:
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        missing = [n for n, v in (("ALPACA_API_KEY", key), ("ALPACA_SECRET_KEY", secret)) if not v]
        if missing:
            raise BrokerError(
                "config_missing",
                "Fehlende Umgebungsvariable(n): " + ", ".join(missing),
                hint=(
                    "Paper-Schlüssel in die lokale .env im Projektordner eintragen (ALPACA_API_KEY=… und "
                    "ALPACA_SECRET_KEY=…) und das Backend neu starten. Ein Network Secret aus Claude Code "
                    "im Web gilt nur in der Cloud, nicht auf diesem Rechner."
                ),
            )
        # Alpaca erwartet die Schlüssel ohne Bearer-Präfix.
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def describe(self) -> dict[str, Any]:
        return {
            "auth_mode": "env",
            "api_key_present": bool(os.environ.get("ALPACA_API_KEY", "").strip()),
            "secret_key_present": bool(os.environ.get("ALPACA_SECRET_KEY", "").strip()),
        }


class MissingAuth(AuthStrategy):
    mode = "unset"

    def headers(self) -> dict[str, str]:
        raise BrokerError(
            "config_missing",
            "ALPACA_AUTH_MODE ist nicht gesetzt oder ungültig (erlaubt: proxy, env).",
            hint="In Claude Code im Web: ALPACA_AUTH_MODE=proxy. Auf dem eigenen Rechner: ALPACA_AUTH_MODE=env.",
        )

    def describe(self) -> dict[str, Any]:
        return {"auth_mode": None}


def auth_from_env() -> AuthStrategy:
    mode = auth_mode_from_env()
    if mode == "proxy":
        return ProxyAuth()
    if mode == "env":
        return EnvAuth()
    return MissingAuth()


# --------------------------------------------------------------------------- #
# URL-Prüfung
# --------------------------------------------------------------------------- #
def build_url(kind: str, path: str) -> httpx.URL:
    if kind not in BASES:
        raise BrokerError("invalid_url", f"Unbekanntes Ziel: {kind}")
    if not path.startswith(PATH_PREFIXES[kind]) or any(t in path for t in ("..", "//", "\\", "?", "#", "@")):
        raise BrokerError("invalid_url", "Ungültiger API-Pfad")
    url = httpx.URL(BASES[kind] + path)
    validate_url(url, kind)
    return url


def validate_url(url: httpx.URL, kind: str | None = None) -> None:
    allowed = {HOSTS[kind]} if kind else ALLOWED_HOSTS
    if (
        url.scheme != "https"
        or url.host not in allowed
        or url.port not in (None, 443)
        or url.userinfo
    ):
        raise BrokerError("invalid_url", f"Zieladresse nicht erlaubt: {url.scheme}://{url.host}")


async def _request_guard(request: httpx.Request) -> None:
    # Zweite Verteidigungslinie: jede ausgehende Anfrage muss an einen der zwei Hosts gehen.
    validate_url(request.url)


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    # Alpaca liefert teils Nanosekunden – auf Mikrosekunden kürzen.
    if "." in text:
        head, tail = text.split(".", 1)
        frac = "".join(ch for ch in tail if ch.isdigit())
        rest = tail[len(frac):]
        text = f"{head}.{frac[:6]}{rest}"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_symbol(symbol: str) -> str:
    """Aktiensymbol für einen URL-Pfad (Krypto wird als Query-Parameter übergeben)."""
    if not STOCK_SYMBOL_RE.match(symbol):
        raise BrokerError("unprocessable", f"Ungültiges Aktiensymbol: {symbol!r}")
    return quote(symbol, safe=".")


def _check_crypto(symbol: str) -> str:
    if not CRYPTO_SYMBOL_RE.match(symbol):
        raise BrokerError("unprocessable", f"Ungültiges Krypto-Symbol: {symbol!r}")
    return symbol


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        msg = None
    msg = (msg or resp.reason_phrase or "").strip()
    return msg[:300]


def parse_account(d: dict) -> Account:
    return Account(
        status=str(d.get("status", "UNKNOWN")),
        currency=str(d.get("currency") or "USD"),
        cash=_f(d.get("cash")) or 0.0,
        equity=_f(d.get("equity")) or _f(d.get("portfolio_value")) or 0.0,
        last_equity=_f(d.get("last_equity")),
        buying_power=_f(d.get("buying_power")),
        non_marginable_buying_power=_f(d.get("non_marginable_buying_power")),
        trading_blocked=bool(d.get("trading_blocked", False)),
        account_blocked=bool(d.get("account_blocked", False)),
        trade_suspended_by_user=bool(d.get("trade_suspended_by_user", False)),
        multiplier=str(d.get("multiplier")) if d.get("multiplier") is not None else None,
    )


def parse_order(d: dict) -> Order:
    return Order(
        id=str(d["id"]),
        client_order_id=str(d.get("client_order_id") or ""),
        symbol=canonical_symbol(str(d.get("symbol") or ""), d.get("asset_class")),
        side=str(d.get("side") or ""),
        type=str(d.get("type") or d.get("order_type") or "market"),
        time_in_force=str(d.get("time_in_force") or "day"),
        qty=_f(d.get("qty")),
        notional=_f(d.get("notional")),
        filled_qty=_f(d.get("filled_qty")) or 0.0,
        filled_avg_price=_f(d.get("filled_avg_price")),
        status=str(d.get("status") or "unknown"),
        created_at=_dt(d.get("created_at")),
        updated_at=_dt(d.get("updated_at")),
        submitted_at=_dt(d.get("submitted_at")),
        filled_at=_dt(d.get("filled_at")),
        canceled_at=_dt(d.get("canceled_at")),
    )


def parse_position(d: dict) -> Position:
    qty = _f(d.get("qty")) or 0.0
    qty_av = _f(d.get("qty_available"))
    return Position(
        symbol=canonical_symbol(str(d.get("symbol") or ""), d.get("asset_class")),
        qty=qty,
        qty_available=qty if qty_av is None else qty_av,
        side=str(d.get("side") or "long"),
        avg_entry_price=_f(d.get("avg_entry_price")) or 0.0,
        market_value=_f(d.get("market_value")) or 0.0,
        cost_basis=_f(d.get("cost_basis")) or 0.0,
        unrealized_pl=_f(d.get("unrealized_pl")) or 0.0,
        unrealized_intraday_pl=_f(d.get("unrealized_intraday_pl")),
        current_price=_f(d.get("current_price")),
        asset_class=str(d.get("asset_class") or "us_equity"),
    )


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class AlpacaPaperBroker(Broker):
    mode = "PAPER"
    uses_sessions = True
    data_feed = DATA_FEED
    data_source_label = "Alpaca Market Data, IEX-Feed (kostenlos; nur Handelsplatz IEX, Teilabdeckung des Marktes)"

    def __init__(
        self,
        auth: AuthStrategy | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_base_s: float = 0.5,
    ) -> None:
        self.auth = auth or auth_from_env()
        self._backoff_base_s = backoff_base_s
        self._session_cache: dict[str, datetime | None] = {}
        kwargs: dict[str, Any] = {}
        if transport is not None:  # nur für Tests (Mock-Transport, kein Netzwerk)
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=False,
            # Übernimmt HTTPS_PROXY und SSL_CERT_FILE der Umgebung; TLS-Prüfung bleibt aktiv.
            trust_env=transport is None,
            headers={"Accept": "application/json", "User-Agent": "alpaca-paper-bot/0.1"},
            event_hooks={"request": [_request_guard]},
            **kwargs,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        kind: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = build_url(kind, path)
        headers = self.auth.headers()
        is_read = method == "GET"
        attempts = READ_ATTEMPTS if is_read else 1
        last_err: BrokerError | None = None
        for attempt in range(attempts):
            err: BrokerError
            try:
                resp = await self._client.request(method, url, params=params, json=json, headers=headers)
            except httpx.ProxyError as exc:
                # Meist eine Richtlinien-Ablehnung (Host nicht freigegeben) → keine Wiederholung.
                log.warning("Proxy-Fehler bei %s %s: %s", method, url.host, type(exc).__name__)
                raise BrokerError(
                    "network",
                    "Der ausgehende Proxy hat die Verbindung abgelehnt.",
                    hint=self._proxy_hint(),
                    may_have_been_sent=False,
                ) from exc
            except (httpx.ConnectTimeout, httpx.PoolTimeout):
                err = BrokerError("timeout", "Zeitüberschreitung beim Verbindungsaufbau.", may_have_been_sent=False)
            except httpx.TimeoutException:
                err = BrokerError(
                    "timeout",
                    "Zeitüberschreitung beim Warten auf die Antwort von Alpaca.",
                    may_have_been_sent=True,
                )
            except httpx.ConnectError:
                err = BrokerError(
                    "network",
                    "Keine Verbindung zu Alpaca möglich (DNS/Netzwerk).",
                    hint=self._proxy_hint(),
                    may_have_been_sent=False,
                )
            except httpx.TransportError as exc:
                err = BrokerError(
                    "network",
                    f"Verbindung unterbrochen ({type(exc).__name__}).",
                    may_have_been_sent=True,
                )
            else:
                status = resp.status_code
                if 200 <= status < 300:
                    if status == 204 or not resp.content:
                        return None
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise BrokerError("invalid_response", "Antwort von Alpaca ist kein gültiges JSON.") from exc
                if 300 <= status < 400:
                    raise BrokerError(
                        "unexpected_redirect",
                        f"Unerwartete Weiterleitung (HTTP {status}) – wird nicht verfolgt.",
                        status_code=status,
                    )
                if status == 429:
                    retry_after = _f(resp.headers.get("Retry-After"))
                    err = BrokerError(
                        "rate_limited",
                        "API-Limit erreicht (HTTP 429).",
                        status_code=429,
                        hint="Abrufintervall erhöhen; der kostenlose Zugang erlaubt ca. 200 Anfragen pro Minute.",
                        retry_after_s=retry_after,
                    )
                elif status >= 500:
                    err = BrokerError(
                        "broker_error",
                        f"Alpaca-Serverfehler (HTTP {status}): {_error_message(resp)}",
                        status_code=status,
                        may_have_been_sent=True,
                    )
                else:
                    raise self._client_error(resp)
            last_err = err
            if is_read and attempt < attempts - 1:
                delay = err.retry_after_s or self._backoff_base_s * (2**attempt)
                delay = min(delay, 10.0) + random.uniform(0, self._backoff_base_s / 4)
                await asyncio.sleep(delay)
                continue
            raise err
        assert last_err is not None  # pragma: no cover
        raise last_err  # pragma: no cover

    def _proxy_hint(self) -> str:
        if self.auth.mode == "proxy":
            return (
                "In Claude Code im Web: Network Secret für paper-api.alpaca.markets und "
                "data.alpaca.markets anlegen (oder die Hosts unter Network access freigeben)."
            )
        return "Internetverbindung, Firewall und Proxy-Einstellungen prüfen."

    def _client_error(self, resp: httpx.Response) -> BrokerError:
        status = resp.status_code
        msg = _error_message(resp)
        if status == 401:
            hint = (
                "Network Secret prüfen: Custom Headers APCA-API-KEY-ID und APCA-API-SECRET-KEY "
                "(ohne Prefix) für beide Hosts."
                if self.auth.mode == "proxy"
                else "ALPACA_API_KEY/ALPACA_SECRET_KEY prüfen – es müssen Paper-Schlüssel sein."
            )
            return BrokerError("auth_rejected", f"Authentifizierung abgelehnt (HTTP 401): {msg}", status_code=401, hint=hint)
        if status == 403:
            return BrokerError(
                "forbidden",
                f"Zugriff verweigert / unzureichende Berechtigung (HTTP 403): {msg}",
                status_code=403,
                hint="Schlüssel/Abo-Berechtigungen und Kontostatus prüfen. Bei Orders: Bestand oder Mittel unzureichend.",
            )
        if status == 404:
            return BrokerError("not_found", f"Nicht gefunden (HTTP 404): {msg}", status_code=404)
        if status == 422:
            return BrokerError("unprocessable", f"Anfrage abgelehnt (HTTP 422): {msg}", status_code=422)
        return BrokerError("broker_error", f"Brokerfehler (HTTP {status}): {msg}", status_code=status)

    # ------------------------------------------------------------------ #
    async def get_account(self) -> Account:
        data = await self._request("GET", "trading", "/v2/account")
        if not isinstance(data, dict):
            raise BrokerError("invalid_response", "Unerwartete Kontoantwort.")
        return parse_account(data)

    async def get_clock(self) -> Clock:
        d = await self._request("GET", "trading", "/v2/clock")
        return Clock(
            timestamp=_dt(d.get("timestamp")) or datetime.now(timezone.utc),
            is_open=bool(d.get("is_open")),
            next_open=_dt(d.get("next_open")),
            next_close=_dt(d.get("next_close")),
        )

    async def get_session_open(self, now: datetime) -> datetime | None:
        """Regulärer Sitzungsbeginn laut Handelskalender (berücksichtigt Feiertage und verkürzte Tage)."""
        day = now.astimezone(NEW_YORK).date()
        key = day.isoformat()
        if key not in self._session_cache:
            data = await self._request("GET", "trading", "/v2/calendar", params={"start": key, "end": key})
            entry = next((d for d in (data or []) if d.get("date") == key), None)
            opened = None
            if entry and entry.get("open"):
                digits = "".join(ch for ch in str(entry["open"]) if ch.isdigit())
                if len(digits) < 4:
                    raise BrokerError("invalid_response", f"Unerwartetes Kalenderformat: {str(entry['open'])[:20]!r}")
                hh, mm = int(digits[:2]), int(digits[2:4])
                opened = datetime.combine(day, time(hh, mm), tzinfo=NEW_YORK).astimezone(timezone.utc)
            self._session_cache = {key: opened}  # nur der aktuelle Tag wird gehalten
        return self._session_cache[key]

    async def get_asset(self, symbol: str) -> Asset:
        if is_crypto(symbol):
            _check_crypto(symbol)
            base = symbol.split("/")[0]
            # Alpaca akzeptiert für Krypto je nach Version "BTCUSD" oder "BTC/USD" (kodiert).
            d = None
            for candidate in (f"{base}USD", quote(symbol, safe="")):
                try:
                    d = await self._request("GET", "trading", f"/v2/assets/{candidate}")
                    break
                except BrokerError as exc:
                    if exc.kind != "not_found":
                        raise
            if d is None:
                raise BrokerError("not_found", f"Krypto-Instrument {symbol} beim Broker nicht gefunden", status_code=404)
        else:
            d = await self._request("GET", "trading", f"/v2/assets/{_check_symbol(symbol)}")
        asset_class = str(d.get("class") or "")
        return Asset(
            symbol=canonical_symbol(str(d.get("symbol") or symbol), asset_class),
            name=str(d.get("name") or ""),
            asset_class=asset_class,
            exchange=str(d.get("exchange") or ""),
            status=str(d.get("status") or ""),
            tradable=bool(d.get("tradable")),
            fractionable=bool(d.get("fractionable")) or asset_class == "crypto",
            shortable=bool(d.get("shortable")),
            marginable=bool(d.get("marginable")),
            min_order_size=_f(d.get("min_order_size")),
            min_trade_increment=_f(d.get("min_trade_increment")),
        )

    async def list_positions(self) -> list[Position]:
        data = await self._request("GET", "trading", "/v2/positions")
        return [parse_position(p) for p in (data or [])]

    async def list_orders(
        self, status: str = "open", limit: int = 100, after: datetime | None = None
    ) -> list[Order]:
        params: dict[str, Any] = {"status": status, "limit": min(limit, 500), "direction": "desc", "nested": "false"}
        if after is not None:
            params["after"] = _rfc3339(after)
        data = await self._request("GET", "trading", "/v2/orders", params=params)
        return [parse_order(o) for o in (data or [])]

    async def get_order(self, order_id: str) -> Order:
        if not UUID_RE.match(order_id):
            raise BrokerError("unprocessable", "Ungültige Order-ID")
        return parse_order(await self._request("GET", "trading", f"/v2/orders/{order_id}"))

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        try:
            d = await self._request(
                "GET",
                "trading",
                "/v2/orders:by_client_order_id",
                params={"client_order_id": client_order_id},
            )
        except BrokerError as exc:
            if exc.kind == "not_found":
                return None
            raise
        return parse_order(d)

    async def submit_order(self, req: OrderRequest) -> Order:
        payload: dict[str, Any] = {
            "symbol": req.symbol,
            "side": req.side,
            "type": "market",
            "time_in_force": req.time_in_force,
            "client_order_id": req.client_order_id,
            "extended_hours": False,
        }
        if req.qty is not None:
            payload["qty"] = format(req.qty, "f")
        if req.notional is not None:
            payload["notional"] = format(req.notional, "f")
        data = await self._request("POST", "trading", "/v2/orders", json=payload)
        return parse_order(data)

    async def cancel_order(self, order_id: str) -> None:
        if not UUID_RE.match(order_id):
            raise BrokerError("unprocessable", "Ungültige Order-ID")
        await self._request("DELETE", "trading", f"/v2/orders/{order_id}")

    @staticmethod
    def _parse_bars(raw: list[dict]) -> list[Bar]:
        bars = []
        for b in raw or []:
            t = _dt(b.get("t"))
            if t is None:
                continue
            bars.append(
                Bar(t=t, o=_f(b.get("o")) or 0, h=_f(b.get("h")) or 0, l=_f(b.get("l")) or 0,
                    c=_f(b.get("c")) or 0, v=_f(b.get("v")) or 0)
            )
        bars.sort(key=lambda x: x.t)
        return bars

    def _bars_endpoint(self, symbol: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if is_crypto(symbol):
            params["symbols"] = _check_crypto(symbol)
            return f"/v1beta3/crypto/{CRYPTO_LOCATION}/bars", params
        params.update(feed=DATA_FEED, adjustment="raw")
        return f"/v2/stocks/{_check_symbol(symbol)}/bars", params

    @staticmethod
    def _bars_from(symbol: str, data: Any) -> list[dict]:
        raw = (data or {}).get("bars") or []
        return (raw.get(symbol) or []) if isinstance(raw, dict) else raw

    async def get_bars(self, symbol: str, limit: int, timeframe: str = "1Min") -> list[Bar]:
        if timeframe not in TIMEFRAMES:
            raise BrokerError("unprocessable", f"Unbekanntes Intervall: {timeframe}")
        now = datetime.now(timezone.utc)
        params: dict[str, Any] = {
            "timeframe": timeframe,
            "start": _rfc3339(now - LOOKBACK[timeframe]),
            "limit": max(1, min(limit, 1000)),
            "sort": "desc",
        }
        path, params = self._bars_endpoint(symbol, params)
        data = await self._request("GET", "data", path, params=params)
        return self._parse_bars(self._bars_from(symbol, data))

    async def get_bars_range(
        self, symbol: str, timeframe: str, start: datetime, end: datetime, max_bars: int
    ) -> list[Bar]:
        if timeframe not in TIMEFRAMES:
            raise BrokerError("unprocessable", f"Unbekanntes Intervall: {timeframe}")
        out: list[Bar] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {
                "timeframe": timeframe,
                "start": _rfc3339(start),
                "end": _rfc3339(end),
                "limit": min(10_000, max_bars - len(out)),
                "sort": "asc",
            }
            if token:
                params["page_token"] = token
            path, params = self._bars_endpoint(symbol, params)
            data = await self._request("GET", "data", path, params=params)
            out.extend(self._parse_bars(self._bars_from(symbol, data)))
            token = (data or {}).get("next_page_token")
            if not token or len(out) >= max_bars:
                break
        return out[:max_bars]

    async def list_assets(self, asset_class: str) -> list[Asset]:
        if asset_class not in ("us_equity", "crypto"):
            return []
        data = await self._request("GET", "trading", "/v2/assets", params={"status": "active", "asset_class": asset_class})
        out = []
        for d in data or []:
            if not d.get("tradable"):
                continue
            cls = str(d.get("class") or asset_class)
            if cls == "crypto" and not str(d.get("symbol", "")).endswith("USD"):
                continue  # nur Paare gegen USD
            out.append(
                Asset(
                    symbol=canonical_symbol(str(d.get("symbol") or ""), cls),
                    name=str(d.get("name") or ""),
                    asset_class=cls,
                    exchange=str(d.get("exchange") or ""),
                    status=str(d.get("status") or ""),
                    tradable=True,
                    fractionable=bool(d.get("fractionable")) or cls == "crypto",
                )
            )
        return out

    async def get_latest_price(self, symbol: str) -> LatestPrice | None:
        if is_crypto(symbol):
            data = await self._request(
                "GET", "data", f"/v1beta3/crypto/{CRYPTO_LOCATION}/latest/trades",
                params={"symbols": _check_crypto(symbol)},
            )
            trade = ((data or {}).get("trades") or {}).get(symbol) or {}
            source = "Krypto letzter Trade"
        else:
            data = await self._request(
                "GET", "data", f"/v2/stocks/{_check_symbol(symbol)}/trades/latest", params={"feed": DATA_FEED}
            )
            trade = (data or {}).get("trade") or {}
            source = "IEX letzter Trade"
        price = _f(trade.get("p"))
        at = _dt(trade.get("t"))
        if not price or at is None:
            return None
        return LatestPrice(price=price, at=at, source=source)

    async def get_quote(self, symbol: str) -> Quote | None:
        if is_crypto(symbol):
            data = await self._request(
                "GET", "data", f"/v1beta3/crypto/{CRYPTO_LOCATION}/latest/quotes",
                params={"symbols": _check_crypto(symbol)},
            )
            q = ((data or {}).get("quotes") or {}).get(symbol) or {}
            source = "Alpaca Krypto (bester Geld-/Briefkurs)"
        else:
            data = await self._request(
                "GET", "data", f"/v2/stocks/{_check_symbol(symbol)}/quotes/latest", params={"feed": DATA_FEED}
            )
            q = (data or {}).get("quote") or {}
            source = "IEX-Quote (nur Handelsplatz IEX)"
        bid, ask, at = _f(q.get("bp")), _f(q.get("ap")), _dt(q.get("t"))
        if not bid or not ask or at is None or ask < bid:
            return None
        return Quote(symbol=symbol, bid=bid, ask=ask, at=at, source=source)
