"""Echtzeit-Datenströme: Protokoll, Rückfall auf Abfrage, Order-Updates (Fake-WebSocket, kein Netzwerk)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from contextlib import asynccontextmanager

import pytest

from app.config import CRYPTO_STREAM_URL, TRADE_STREAM_URL
from app.core import AppContext
from app.settings_store import BotParams, set_bot_params
from app.streams import SOURCE_STREAM, CryptoQuoteStream, StreamError, TradeUpdateStream, _NoRedirectConnect

KEY, SECRET = "PKTESTKEYID", "testsecretvalue"  # wie env_keys in conftest
SCALP = dict(strategy="scalp", atr_exits=False, trend_filter=False, symbol="BTC/USD", symbols=["BTC/USD"], short_window=2, long_window=4, bar_seconds=5,
             poll_interval_s=1, order_value=20, broker_stop=False)
DOWN_UP = [100, 99.8, 99.6, 99.4, 99.2, 99.0, 99.9, 100.0]


class FakeWS:
    def __init__(self, server: FakeStreams, url: str) -> None:
        self.server, self.url = server, url
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        msg = json.loads(message)
        self.sent.append(msg)
        self.server.on_send(self, msg)

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def push(self, obj, binary: bool = False) -> None:
        data = json.dumps(obj)
        self.inbox.put_nowait(data.encode() if binary else data)


class FakeStreams:
    """Antwortet wie Alpacas Krypto-Datenstrom bzw. Order-Strom (laut offiziellem SDK)."""

    def __init__(self) -> None:
        self.sockets: list[FakeWS] = []
        self.fail_connects = 0
        self.data_error: int | None = None  # Fehlercode statt „authenticated“

    def connector(self, url: str):
        server = self

        @asynccontextmanager
        async def cm():
            if server.fail_connects:
                server.fail_connects -= 1
                raise OSError("Verbindung abgelehnt")
            ws = FakeWS(server, url)
            server.sockets.append(ws)
            if url == CRYPTO_STREAM_URL:
                ws.push([{"T": "success", "msg": "connected"}])
            try:
                yield ws
            finally:
                ws.closed = True

        return cm()

    def on_send(self, ws: FakeWS, msg: dict) -> None:
        action = msg.get("action")
        if action == "auth":
            if self.data_error:
                ws.push([{"T": "error", "code": self.data_error, "msg": "x"}])
            elif msg.get("key") == KEY and msg.get("secret") == SECRET:
                ws.push([{"T": "success", "msg": "authenticated"}])
            else:
                ws.push([{"T": "error", "code": 402, "msg": "auth failed"}])
        elif action == "subscribe":
            ws.push([{"T": "subscription", "trades": [], "quotes": msg.get("quotes", []), "bars": []}])
        elif action == "authenticate":
            ok = msg["data"] == {"key_id": KEY, "secret_key": SECRET}
            ws.push({"stream": "authorization", "data": {"status": "authorized" if ok else "unauthorized",
                                                          "action": "authenticate"}}, binary=True)
        elif action == "listen":
            ws.push({"stream": "listening", "data": {"streams": ["trade_updates"]}}, binary=True)

    def ws(self, url: str) -> FakeWS:
        return [w for w in self.sockets if w.url == url][-1]

    def quote(self, sym: str, bid: float, ask: float, at) -> None:
        self.ws(CRYPTO_STREAM_URL).push(
            [{"T": "q", "S": sym, "bp": bid, "bs": 1.2, "ap": ask, "as": 0.8, "t": at.isoformat().replace("+00:00", "Z")}]
        )

    def trade_update(self, event: str, order: dict) -> None:
        self.ws(TRADE_STREAM_URL).push({"stream": "trade_updates", "data": {
            "event": event, "order": order, "timestamp": order.get("updated_at"),
            "price": order.get("filled_avg_price"), "qty": order.get("filled_qty")}}, binary=True)


async def settle(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)


@pytest.fixture
def streams() -> FakeStreams:
    return FakeStreams()


def creds():
    return KEY, SECRET


# ------------------------------------------------------------ Kursstrom
async def test_crypto_stream_handshake_subscribe_and_quotes(streams, clock):
    woke: list[int] = []
    s = CryptoQuoteStream(creds, connector=streams.connector, now=clock, on_quote=lambda: woke.append(1))
    await s.set_symbols(["BTC/USD", "ETH/USD"])
    s.start()
    await settle()
    try:
        ws = streams.ws(CRYPTO_STREAM_URL)
        assert s.live and ws.sent[0] == {"action": "auth", "key": KEY, "secret": SECRET}
        assert ws.sent[1] == {"action": "subscribe", "quotes": ["BTC/USD", "ETH/USD"]}
        assert s.subscribed == ["BTC/USD", "ETH/USD"]
        streams.quote("BTC/USD", 99.99, 100.01, clock())
        streams.quote("DOGE/USD", 1, 1.1, clock())  # nicht abonniert → ignoriert
        streams.quote("ETH/USD", 10.0, 9.0, clock())  # Brief unter Geld → ignoriert
        await settle()
        q = s.fresh_quote("BTC/USD", clock(), 15)
        assert q is not None and q.bid == 99.99 and q.source == SOURCE_STREAM
        assert s.fresh_quote("ETH/USD", clock(), 15) is None and "DOGE/USD" not in s.quotes
        assert s.drain("BTC/USD") == [(clock(), 100.0)] and s.drain("BTC/USD") == []
        assert woke == [1]
        clock.advance(seconds=16)
        assert s.fresh_quote("BTC/USD", clock(), 15) is None  # zu alt
        await s.set_symbols(["ETH/USD", "SOL/USD"])
        assert ws.sent[-2:] == [{"action": "unsubscribe", "quotes": ["BTC/USD"]},
                                {"action": "subscribe", "quotes": ["SOL/USD"]}]
        assert "BTC/USD" not in s.quotes
    finally:
        await s.stop()
    assert s.state == "off" and not s.running


@pytest.mark.parametrize(("code", "text", "wait"), [
    (402, "Anmeldung abgelehnt", 300.0),
    (406, "Verbindungslimit erreicht", 60.0),
])
async def test_data_stream_errors_back_off_without_leaking_keys(streams, clock, code, text, wait):
    streams.data_error = code
    sleeps = Sleeps()
    s = CryptoQuoteStream(creds, connector=streams.connector, now=clock, sleep=sleeps)
    await s.set_symbols(["BTC/USD"])
    s.start()
    await settle()
    try:
        assert s.state == "reconnecting" and text in s.last_error and f"Code {code}" in s.last_error
        assert sleeps.calls[0] == wait
        dumped = json.dumps(s.describe())
        assert SECRET not in dumped and KEY not in dumped
    finally:
        await s.stop()


async def test_reconnects_after_failures_and_drop(streams, clock):
    streams.fail_connects = 2
    sleeps = Sleeps()
    s = CryptoQuoteStream(creds, connector=streams.connector, now=clock, sleep=sleeps)
    await s.set_symbols(["BTC/USD"])
    s.start()
    await settle(30)
    try:
        assert s.live and s.failures == 2 and len(sleeps.calls) == 2 and sleeps.calls[0] <= sleeps.calls[1] <= 30
        assert "Verbindung abgelehnt" in s.last_error
        streams.ws(CRYPTO_STREAM_URL).inbox.put_nowait(ConnectionError("getrennt"))
        await settle(30)
        assert s.live and s.connects == 2 and len(streams.sockets) == 2
        assert streams.sockets[-1].sent[1] == {"action": "subscribe", "quotes": ["BTC/USD"]}  # erneut abonniert
    finally:
        await s.stop()


async def test_silent_stream_is_treated_as_stale(streams, clock):
    sleeps = Sleeps()
    s = CryptoQuoteStream(creds, connector=streams.connector, now=clock, sleep=sleeps)
    s.stale_after_s = 0.05
    await s.set_symbols(["BTC/USD"])
    s.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if s.failures:
                break
        assert s.failures >= 1 and "keine Daten" in (s.last_error or "")
    finally:
        await s.stop()


async def test_without_keys_stream_is_unavailable(streams, clock):
    s = CryptoQuoteStream(lambda: None, connector=streams.connector, now=clock)
    s.start()
    await settle()
    assert s.state == "unavailable" and "Proxy-Modus" in s.last_error and not streams.sockets


def test_redirects_are_never_followed():
    exc = StreamError("x")
    assert _NoRedirectConnect.process_redirect(object(), exc) is exc  # type: ignore[arg-type]


# ------------------------------------------------------------ Order-Strom
async def test_trade_stream_binary_frames_and_auth(streams, clock):
    got: list[tuple[str, str, str]] = []
    t = TradeUpdateStream(creds, connector=streams.connector, now=clock, on_order=lambda o, e: got.append((e, o.symbol, o.status)))
    t.start()
    await settle()
    try:
        ws = streams.ws(TRADE_STREAM_URL)
        assert t.live and ws.sent == [
            {"action": "authenticate", "data": {"key_id": KEY, "secret_key": SECRET}},
            {"action": "listen", "data": {"streams": ["trade_updates"]}},
        ]
        streams.trade_update("fill", {"id": "o1", "client_order_id": "c1", "symbol": "BTCUSD", "asset_class": "crypto",
                                      "side": "buy", "status": "filled", "filled_qty": "0.1", "filled_avg_price": "100"})
        await settle()
        assert got == [("fill", "BTC/USD", "filled")] and t.last_event == "fill BTC/USD"
    finally:
        await t.stop()


async def test_trade_stream_rejected_auth(streams, clock):
    sleeps = Sleeps()
    t = TradeUpdateStream(lambda: (KEY, "falsch"), connector=streams.connector, now=clock, sleep=sleeps)
    t.start()
    await settle()
    try:
        assert t.state == "reconnecting" and "abgelehnt" in t.last_error and sleeps.calls[0] == 300.0
    finally:
        await t.stop()


# ------------------------------------------------------------ Bot mit Strom
@pytest.fixture
async def sctx(settings, db, paper_broker, clock, streams):
    c = AppContext(dataclasses.replace(settings, streams=True), db, paper_broker=paper_broker, now=clock,
                   stream_connector=streams.connector)
    await c.startup(background=False)
    await c.set_mode("PAPER")  # startet den Order-Strom
    c.set_orders_enabled("PAPER", True)
    await settle()
    yield c
    await c.shutdown()


async def start_bot(ctx: AppContext, fake, **kw) -> None:
    set_bot_params(ctx.db, "PAPER", BotParams(**{**SCALP, **kw}))
    for s in kw.get("symbols", SCALP["symbols"]):
        fake.set_quote(s, 99.99, 100.01)
    await ctx.bot.start("PAPER")
    ctx.bot._task.cancel()
    await settle()


async def test_bot_uses_stream_quotes_and_falls_back_to_polling(sctx: AppContext, fake, streams, clock):
    await start_bot(sctx, fake)
    market = sctx.streams.market
    assert market.live and market.symbols == ["BTC/USD"]
    assert any("Echtzeit-Datenstrom" in e["message"] for e in sctx.db.list_events("PAPER", 10))
    path = "/v1beta3/crypto/us/latest/quotes"
    for mid in DOWN_UP:
        clock.advance(seconds=5)
        streams.quote("BTC/USD", round(mid - 0.01, 4), round(mid + 0.01, 4), clock())
        await settle()
        before, buys = fake.count("GET", path), len([o for o in fake.orders.values() if o["side"] == "buy"])
        await sctx.bot.tick("PAPER")
        if len([o for o in fake.orders.values() if o["side"] == "buy"]) == buys:
            assert fake.count("GET", path) == before  # Kurse aus dem Strom, keine Abfrage
    polls = fake.count("GET", path)  # (die Ordervorprüfung vor einem Kauf fragt den Kurs bewusst direkt ab)
    view = sctx.bot.scalper.state["symbols"]["BTC/USD"]
    assert view["quote"]["via"] == "stream" and sctx.bot.scalper.state["feed"] == "stream"
    assert [o for o in fake.orders.values() if o["side"] == "buy"], "Einstieg aus Strom-Kursen erwartet"

    # Strom bricht ab → Abfrage übernimmt sofort
    market._sleep = lambda _s: asyncio.Event().wait()  # Wartezeit bis zum Neuversuch läuft im Test nicht ab
    streams.ws(CRYPTO_STREAM_URL).inbox.put_nowait(ConnectionError("weg"))
    await settle(20)
    assert market.state == "reconnecting" and "weg" in market.last_error
    clock.advance(seconds=5)
    fake.set_quote("BTC/USD", 100.0, 100.02)
    await sctx.bot.tick("PAPER")
    assert fake.count("GET", "/v1beta3/crypto/us/latest/quotes") == polls + 1
    assert sctx.bot.scalper.state["feed"] == "poll"
    assert sctx.bot.scalper.state["symbols"]["BTC/USD"]["quote"]["via"] == "poll"


async def test_pause_frees_the_market_stream(sctx: AppContext, fake):
    await start_bot(sctx, fake)
    assert sctx.streams.market.live
    await sctx.bot.pause()
    assert sctx.streams.market.state == "off" and not sctx.streams.market.running
    assert sctx.streams.orders.live  # Order-Strom bleibt im PAPER-Modus aktiv


async def test_order_update_finishes_entry_without_waiting_for_sync(sctx: AppContext, fake, streams, clock):
    await start_bot(sctx, fake)
    for mid in DOWN_UP:
        clock.advance(seconds=5)
        streams.quote("BTC/USD", round(mid - 0.01, 4), round(mid + 0.01, 4), clock())
        await settle()
        await sctx.bot.tick("PAPER")
    entry = [o for o in fake.orders.values() if o["side"] == "buy"][0]
    assert sctx.bot.scalper.list_trades("PAPER")[0]["status"] == "entering"
    syncs = fake.count("GET", "/v2/account")
    sctx.bot._wake.clear()
    clock.advance(seconds=6)
    fake.fill(entry, 0.2, 100.0)
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    streams.trade_update("fill", entry)
    await settle(40)
    local = sctx.db.query_one("SELECT status FROM orders WHERE client_order_id=?", (entry["client_order_id"],))
    assert local["status"] == "filled"
    assert fake.count("GET", "/v2/account") == syncs + 1  # sofortiger Abgleich
    assert sctx.bot.scalper.list_trades("PAPER")[0]["status"] == "open"
    assert sctx.bot._wake.is_set()  # Bot läuft sofort weiter (setzt dann die Schutzorder)


async def test_demo_mode_never_opens_streams(ctx: AppContext, streams):
    assert ctx.mode == "DEMO"
    await ctx.streams.orders_for("DEMO")
    await ctx.streams.market_for("DEMO", ["BTC/USD"])
    assert not streams.sockets and ctx.streams.market.state == "off"


# ------------------------------------------------------------ echte WebSocket-Bibliothek (lokal)
async def test_real_websocket_connection_and_no_redirect(clock, monkeypatch):
    """Mit der echten websockets-Bibliothek gegen einen lokalen Server (statt Alpaca)."""
    from http import HTTPStatus

    from websockets.asyncio.server import serve

    from app.streams import default_connector

    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    async def handler(conn):
        await conn.send(json.dumps([{"T": "success", "msg": "connected"}]))
        auth = json.loads(await conn.recv())
        ok = auth == {"action": "auth", "key": KEY, "secret": SECRET}
        await conn.send(json.dumps([{"T": "success", "msg": "authenticated"}] if ok else [{"T": "error", "code": 402}]))
        sub = json.loads(await conn.recv())
        await conn.send(json.dumps([{"T": "subscription", "quotes": sub["quotes"]}]))
        await conn.send(json.dumps([{"T": "q", "S": "BTC/USD", "bp": 99.5, "ap": 100.5,
                                     "t": "2026-10-30T14:30:05.123456789Z"}]))
        await conn.wait_closed()

    def redirect(conn, request):
        if request.path == "/umleitung":
            resp = conn.respond(HTTPStatus.FOUND, "")
            resp.headers["Location"] = "ws://127.0.0.1:1/anderswo"
            return resp
        return None

    async with serve(handler, "127.0.0.1", 0, process_request=redirect) as server:
        port = server.sockets[0].getsockname()[1]
        s = CryptoQuoteStream(creds, now=clock)  # Standard-Connector
        s.url = f"ws://127.0.0.1:{port}/v1beta3/crypto/us"
        await s.set_symbols(["BTC/USD"])
        s.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if s.quotes:
                    break
            assert s.live and s.quotes["BTC/USD"].ask == 100.5 and s.subscribed == ["BTC/USD"]
        finally:
            await s.stop()

        with pytest.raises(Exception) as info:
            async with default_connector(f"ws://127.0.0.1:{port}/umleitung"):
                pass
        assert "302" in str(info.value)  # Weiterleitung wird nicht verfolgt


async def test_keys_never_reach_the_log(streams, clock, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    s = CryptoQuoteStream(creds, connector=streams.connector, now=clock)
    await s.set_symbols(["BTC/USD"])
    s.start()
    await settle()
    await s.stop()
    assert logging.getLogger("websockets").level == logging.WARNING
    assert SECRET not in caplog.text and KEY not in caplog.text
