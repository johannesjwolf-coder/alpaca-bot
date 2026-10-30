"""Echtzeit-Datenströme (WebSocket) von Alpaca – mit automatischem Rückfall auf die Abfrage.

* **Krypto-Kurse:** ``wss://stream.data.alpaca.markets/v1beta3/crypto/us`` – bester Geld-/Briefkurs
  je Paar, sobald er sich ändert. Läuft nur, solange der Scalping-Bot im PAPER-Modus arbeitet
  (der kostenlose Zugang erlaubt eine gleichzeitige Verbindung).
* **Order-Updates:** ``wss://paper-api.alpaca.markets/stream`` (``trade_updates``) – Ausführungen
  und Stornierungen des Paper-Kontos sofort statt erst beim nächsten Abgleich. Läuft im PAPER-Modus.

Beide Adressen sind fest (``config.py``); Weiterleitungen werden nicht verfolgt. Die Ströme gibt es
nur mit ``ALPACA_AUTH_MODE=env``: Die Schlüssel werden ausschliesslich in der Anmeldenachricht über
die verschlüsselte Verbindung gesendet und nie geloggt. Fällt ein Strom aus, arbeitet der Bot mit
der bisherigen Abfrage alle paar Sekunden weiter und verbindet sich mit wachsender Wartezeit neu.

Protokoll laut offiziellem Alpaca-SDK (alpaca-py): Datenstrom mit JSON-Arrays
(``[{"T":"success","msg":"connected"}]`` → ``{"action":"auth",…}`` → ``{"action":"subscribe",
"quotes":[…]}`` → ``{"T":"q","S":…,"bp":…,"ap":…,"t":…}``), Order-Strom mit
``{"action":"authenticate",…}`` → ``{"action":"listen",…}`` → ``{"stream":"trade_updates",…}``
(Paper sendet Binärframes mit JSON).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import Any, Protocol

from websockets.asyncio.client import connect as _ws_connect

from .broker.alpaca import _dt, _f, parse_order
from .broker.base import Order, Quote
from .config import CRYPTO_STREAM_URL, TRADE_STREAM_URL
from .db import iso
from .runtime import apply_broker_order
from .timeutil import utcnow

if TYPE_CHECKING:
    from .core import AppContext

log = logging.getLogger("alpaca_bot.streams")
# Die Bibliothek protokolliert auf DEBUG-Stufe ganze Frames – dazu gehört die Anmeldenachricht mit den
# Schlüsseln. Deshalb nie unter WARNING, auch wenn jemand global DEBUG einschaltet.
logging.getLogger("websockets").setLevel(logging.WARNING)

STALE_AFTER_S = 30.0  # Kursstrom ohne jede Nachricht → neu verbinden
MAX_SAMPLES = 2000
SOURCE_STREAM = "Alpaca Krypto-Datenstrom (Echtzeit)"


class StreamError(Exception):
    """Fehler mit nutzerverständlicher Meldung (nie mit Schlüsseln)."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_s = retry_after_s


# Fehlercodes des Datenstroms → (Meldung, Wartezeit bis zum nächsten Versuch)
DATA_ERRORS: dict[int, tuple[str, float]] = {
    401: ("nicht angemeldet", 30.0),
    402: ("Anmeldung abgelehnt – Schlüssel prüfen", 300.0),
    404: ("Anmeldung zu langsam (Zeitüberschreitung)", 10.0),
    405: ("zu viele Symbole für den Zugang", 300.0),
    406: ("Verbindungslimit erreicht – der kostenlose Zugang erlaubt nur einen Datenstrom gleichzeitig", 60.0),
    409: ("Zugang umfasst diesen Datenstrom nicht", 600.0),
}


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


Connector = Callable[[str], AbstractAsyncContextManager[WebSocketLike]]


class _NoRedirectConnect(_ws_connect):
    """Wie ``websockets.connect``, aber Weiterleitungen werden nie verfolgt (feste Zieladressen)."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


def default_connector(url: str) -> AbstractAsyncContextManager[WebSocketLike]:
    return _NoRedirectConnect(
        url, open_timeout=10, ping_interval=15, ping_timeout=20, close_timeout=5, max_size=2**20,
        user_agent_header="alpaca-paper-bot",
    )


def _decode(raw: str | bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise StreamError("unlesbare Nachricht vom Datenstrom") from exc


def _safe_error(exc: BaseException) -> str:
    """Kurzbeschreibung ohne Nachrichteninhalte (die nie Schlüssel enthalten, aber sicher ist sicher)."""
    if isinstance(exc, StreamError):
        return exc.message
    if isinstance(exc, TimeoutError):
        return "Zeitüberschreitung"
    name = type(exc).__name__
    detail = str(exc).split("\n")[0][:160]
    return f"{name}: {detail}" if detail else name


class _StreamClient:
    """Gemeinsame Verbindungslogik: verbinden, anmelden, lesen, bei Fehlern mit Backoff neu verbinden."""

    name = "stream"
    url = ""
    stale_after_s: float | None = None

    def __init__(
        self,
        credentials: Callable[[], tuple[str, str] | None],
        *,
        connector: Connector | None = None,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._credentials = credentials
        self._connector = connector or default_connector
        self._now = now
        self._sleep = sleep
        self._task: asyncio.Task | None = None
        self._ws: WebSocketLike | None = None
        self.state = "off"  # off | connecting | live | reconnecting | unavailable
        self.connected_since: datetime | None = None
        self.last_message_at: datetime | None = None
        self.last_error: str | None = None
        self.retry_at: datetime | None = None
        self.connects = 0
        self.failures = 0

    @property
    def live(self) -> bool:
        return self.state == "live"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def describe(self) -> dict:
        return {
            "state": self.state,
            "url": self.url,
            "connected_since": iso(self.connected_since),
            "last_message_at": iso(self.last_message_at),
            "last_error": self.last_error,
            "retry_at": iso(self.retry_at),
            "connects": self.connects,
            "failures": self.failures,
        }

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name=f"stream-{self.name}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._ws = None
        self.state = "off"
        self.connected_since = None
        self.retry_at = None

    async def _run(self) -> None:
        attempt = 0
        while True:
            creds = self._credentials()
            if creds is None:
                self.state = "unavailable"
                self.last_error = "Keine Schlüssel im Backend (Proxy-Modus) – Abfrage statt Datenstrom"
                return
            self.state = "connecting" if attempt == 0 else "reconnecting"
            retry_after: float | None = None
            try:
                async with self._connector(self.url) as ws:
                    self._ws = ws
                    await self._handshake(ws, *creds)
                    self.state = "live"
                    self.connected_since = self._now()
                    self.connects += 1
                    self.retry_at = None
                    attempt = 0
                    log.info("Datenstrom %s verbunden", self.name)
                    await self._consume(ws)
            except asyncio.CancelledError:
                raise
            except StreamError as exc:
                self.last_error = exc.message
                retry_after = exc.retry_after_s
            except Exception as exc:  # noqa: BLE001 - Verbindungsfehler aller Art → neu verbinden
                self.last_error = _safe_error(exc)
            finally:
                self._ws = None
                self.connected_since = None
            self.failures += 1
            attempt += 1
            delay = retry_after or min(30.0, 2.0 ** min(attempt, 5)) * random.uniform(0.75, 1.0)
            self.state = "reconnecting"
            self.retry_at = self._now() + timedelta(seconds=delay)
            log.warning("Datenstrom %s getrennt (%s) – neuer Versuch in %.0f s", self.name, self.last_error, delay)
            await self._sleep(delay)

    async def _recv(self, ws: WebSocketLike) -> Any:
        if self.stale_after_s is None:
            raw = await ws.recv()
        else:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_after_s)
            except TimeoutError as exc:
                raise StreamError(f"keine Daten seit {self.stale_after_s:.0f} s") from exc
        self.last_message_at = self._now()
        return _decode(raw)

    async def _handshake(self, ws: WebSocketLike, key: str, secret: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def _consume(self, ws: WebSocketLike) -> None:  # pragma: no cover
        raise NotImplementedError


class CryptoQuoteStream(_StreamClient):
    name = "kurse"
    url = CRYPTO_STREAM_URL
    stale_after_s = STALE_AFTER_S

    def __init__(self, *args: Any, on_quote: Callable[[], None] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.symbols: list[str] = []
        self.subscribed: list[str] = []
        self.quotes: dict[str, Quote] = {}
        self.received: dict[str, datetime] = {}
        self._samples: dict[str, list[tuple[datetime, float]]] = {}
        self._on_quote = on_quote

    def describe(self) -> dict:
        return {**super().describe(), "symbols": list(self.symbols), "subscribed": list(self.subscribed)}

    # -------------------------------------------------------- Bot-Seite
    async def set_symbols(self, symbols: list[str]) -> None:
        wanted = list(dict.fromkeys(symbols))
        if wanted == self.symbols:
            return
        added = [s for s in wanted if s not in self.symbols]
        removed = [s for s in self.symbols if s not in wanted]
        self.symbols = wanted
        for s in removed:
            self.quotes.pop(s, None)
            self.received.pop(s, None)
            self._samples.pop(s, None)
        ws = self._ws
        if ws is not None and self.live:
            if removed:
                await ws.send(json.dumps({"action": "unsubscribe", "quotes": removed}))
            if added:
                await ws.send(json.dumps({"action": "subscribe", "quotes": added}))

    def fresh_quote(self, symbol: str, now: datetime, max_age_s: float) -> Quote | None:
        """Kurs aus dem Strom, wenn der Strom läuft und der Kurs jung genug ist."""
        q = self.quotes.get(symbol)
        if not self.live or q is None:
            return None
        return q if (now - q.at).total_seconds() <= max_age_s else None

    def drain(self, symbol: str) -> list[tuple[datetime, float]]:
        """Seit dem letzten Abruf empfangene Mittelkurse (Empfangszeit, Kurs)."""
        return self._samples.pop(symbol, [])

    # ---------------------------------------------------------- Protokoll
    def _check_error(self, msg: dict) -> None:
        if msg.get("T") == "error":
            code = int(msg.get("code") or 0)
            text, wait = DATA_ERRORS.get(code, (str(msg.get("msg") or "Fehler"), 30.0))
            raise StreamError(f"{text} (Code {code})", retry_after_s=wait)

    async def _expect(self, ws: WebSocketLike, wanted: str) -> None:
        msgs = await self._recv(ws)
        for msg in msgs if isinstance(msgs, list) else [msgs]:
            self._check_error(msg)
            if msg.get("T") == "success" and msg.get("msg") == wanted:
                return
        raise StreamError(f"unerwartete Antwort statt „{wanted}“")

    async def _handshake(self, ws: WebSocketLike, key: str, secret: str) -> None:
        await self._expect(ws, "connected")
        await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
        await self._expect(ws, "authenticated")
        self.subscribed = []
        if self.symbols:
            await ws.send(json.dumps({"action": "subscribe", "quotes": self.symbols}))

    async def _consume(self, ws: WebSocketLike) -> None:
        while True:
            msgs = await self._recv(ws)
            got_quote = False
            for msg in msgs if isinstance(msgs, list) else [msgs]:
                kind = msg.get("T")
                if kind == "q":
                    got_quote = self._on_quote_msg(msg) or got_quote
                elif kind == "subscription":
                    self.subscribed = [str(s) for s in msg.get("quotes") or []]
                else:
                    self._check_error(msg)
            if got_quote and self._on_quote is not None:
                self._on_quote()

    def _on_quote_msg(self, msg: dict) -> bool:
        sym = str(msg.get("S") or "")
        if sym not in self.symbols:
            return False
        bid, ask, at = _f(msg.get("bp")), _f(msg.get("ap")), _dt(msg.get("t"))
        if not bid or not ask or at is None or ask < bid:
            return False
        q = Quote(symbol=sym, bid=bid, ask=ask, at=at, source=SOURCE_STREAM)
        received = self._now()
        self.quotes[sym] = q
        self.received[sym] = received
        samples = self._samples.setdefault(sym, [])
        samples.append((received, q.mid))
        del samples[:-MAX_SAMPLES]
        return True


class TradeUpdateStream(_StreamClient):
    name = "orders"
    url = TRADE_STREAM_URL
    stale_after_s = None  # ruhige Phasen ohne Orders sind normal; Ping hält die Verbindung

    def __init__(self, *args: Any, on_order: Callable[[Order, str], None] | None = None, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.events = 0
        self.last_event: str | None = None
        self._on_order = on_order

    def describe(self) -> dict:
        return {**super().describe(), "events": self.events, "last_event": self.last_event}

    async def _handshake(self, ws: WebSocketLike, key: str, secret: str) -> None:
        await ws.send(json.dumps({"action": "authenticate", "data": {"key_id": key, "secret_key": secret}}))
        msg = await self._recv(ws)
        data = (msg or {}).get("data") or {}
        if (msg or {}).get("stream") != "authorization" or data.get("status") != "authorized":
            raise StreamError("Anmeldung am Order-Strom abgelehnt – Schlüssel prüfen", retry_after_s=300.0)
        await ws.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))

    async def _consume(self, ws: WebSocketLike) -> None:
        while True:
            msg = await self._recv(ws)
            if not isinstance(msg, dict) or msg.get("stream") != "trade_updates":
                continue
            data = msg.get("data") or {}
            raw_order = data.get("order")
            if not isinstance(raw_order, dict) or not raw_order.get("id"):
                continue
            try:
                order = parse_order(raw_order)
            except (KeyError, TypeError, ValueError):
                continue
            event = str(data.get("event") or "")
            self.events += 1
            self.last_event = f"{event} {order.symbol}"
            if self._on_order is not None:
                self._on_order(order, event)


class StreamManager:
    """Startet/stoppt die Ströme passend zu Modus und Bot und verteilt ihre Ereignisse."""

    def __init__(
        self,
        ctx: AppContext,
        *,
        connector: Connector | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.ctx = ctx
        self.enabled = bool(ctx.settings.streams)
        broker = ctx.runtimes["PAPER"].broker
        self._credentials: Callable[[], tuple[str, str] | None] = getattr(broker, "stream_credentials", lambda: None)
        common: dict[str, Any] = {"connector": connector, "now": ctx._now, "sleep": sleep}
        self.market = CryptoQuoteStream(self._credentials, on_quote=self._on_quote, **common)
        self.orders = TradeUpdateStream(self._credentials, on_order=self._on_order, **common)
        self._followup: asyncio.Task | None = None
        self._again = False

    @property
    def available(self) -> bool:
        return self.enabled and self._credentials() is not None

    def describe(self) -> dict:
        if not self.enabled:
            note = "Abgeschaltet (APP_STREAMS=off) – Abfrage alle paar Sekunden."
        elif self._credentials() is None:
            note = "Nur mit ALPACA_AUTH_MODE=env und Schlüsseln im Backend – hier Abfrage alle paar Sekunden."
        else:
            note = "Kursstrom läuft, solange der Scalping-Bot im PAPER-Modus arbeitet; Order-Strom im PAPER-Modus."
        return {"enabled": self.enabled, "available": self.available, "note": note,
                "market": self.market.describe(), "orders": self.orders.describe()}

    async def market_for(self, mode: str, symbols: list[str] | None) -> None:
        """Kursstrom an für PAPER-Scalping mit diesen Symbolen, sonst aus (gibt die eine Verbindung frei)."""
        if not self.available or mode != "PAPER" or not symbols:
            if self.market.state != "off":
                await self.market.stop()
            return
        await self.market.set_symbols(symbols)
        if not self.market.running:
            self.market.start()

    async def orders_for(self, mode: str) -> None:
        if self.available and mode == "PAPER":
            if not self.orders.running:
                self.orders.start()
        elif self.orders.state != "off":
            await self.orders.stop()

    async def stop(self) -> None:
        await self.market.stop()
        await self.orders.stop()
        if self._followup is not None and not self._followup.done():
            self._followup.cancel()

    # ------------------------------------------------------------ Ereignisse
    def _on_quote(self) -> None:
        self.ctx.bot.wake()

    def _on_order(self, order: Order, event: str) -> None:
        apply_broker_order(self.ctx.db, "PAPER", order, self.ctx._now())
        if self.ctx.mode != "PAPER":
            return
        # Folgeschritte gebündelt: Kontoabgleich (Positionen), Trades abschliessen, Bot sofort weiter.
        if self._followup is None or self._followup.done():
            self._followup = asyncio.create_task(self._follow_up(), name="stream-order-followup")
        else:
            self._again = True

    async def _follow_up(self) -> None:
        while True:
            self._again = False
            try:
                if await self.ctx.runtimes["PAPER"].sync():
                    await self.ctx.bot.scalper.finalize_all("PAPER")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Abgleich nach Order-Update fehlgeschlagen")
            self.ctx.bot.wake()
            if not self._again:
                return
