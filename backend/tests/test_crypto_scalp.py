"""Krypto-Unterstützung und Scalping-Engine (Mock-Tests, keine echten Orders)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.bot import BotStartError
from app.broker.alpaca import build_url
from app.broker.base import BrokerError
from app.core import AppContext
from app.orders import OrderInput
from app.settings_store import BotParams, RiskLimits, set_bot_params, set_risk_limits

SCALP = dict(strategy="scalp", symbol="BTC/USD", symbols=["BTC/USD"], short_window=2, long_window=4, bar_seconds=5,
             poll_interval_s=1, order_value=20, stop_loss_pct=0.5, take_profit_pct=1.0, max_hold_min=1, fee_pct=0.25,
             broker_stop=False)


def failed(preview: dict) -> dict[str, str]:
    return {c["name"]: c["detail"] for c in preview["checks"] if c["blocking"] and not c["ok"]}


def crypto_buy(notional: str = "20", tif: str = "gtc") -> OrderInput:
    return OrderInput(symbol="BTC/USD", side="buy", notional=Decimal(notional), time_in_force=tif)


# ------------------------------------------------------------ Adapter
def test_crypto_paths_only_on_data_host_and_us_location():
    assert str(build_url("data", "/v1beta3/crypto/us/latest/quotes")).startswith("https://data.alpaca.markets/")
    for kind, path in [("data", "/v1beta3/crypto/eu/latest/quotes"), ("trading", "/v1beta3/crypto/us/bars"),
                       ("data", "/v1beta1/news")]:
        with pytest.raises(BrokerError):
            build_url(kind, path)


async def test_crypto_quote_bars_and_symbol_normalization(paper_broker, fake, clock):
    fake.set_quote("BTC/USD", 99.99, 100.01)
    q = await paper_broker.get_quote("BTC/USD")
    assert (q.bid, q.ask) == (99.99, 100.01) and round(q.spread_pct, 3) == 0.02
    fake.make_bars("BTC/USD", [1.0, 2.0, 3.0])
    assert [b.c for b in await paper_broker.get_bars("BTC/USD", 10)] == [1.0, 2.0, 3.0]
    fake.add_position("BTCUSD", 0.5, 100.0, asset_class="crypto")  # Alpaca liefert Positionen ohne Schrägstrich
    assert [p.symbol for p in await paper_broker.list_positions()] == ["BTC/USD"]
    asset = await paper_broker.get_asset("BTC/USD")
    assert asset.asset_class == "crypto" and asset.symbol == "BTC/USD" and asset.min_order_size == 0.0001
    assert ("GET", "/v2/assets/BTCUSD") in [(m, p) for m, p, _ in fake.calls]


async def test_crypto_asset_lookup_falls_back_to_encoded_symbol(paper_broker, fake):
    fake.assets["BTC%2FUSD"] = fake.assets.pop("BTCUSD")
    asset = await paper_broker.get_asset("BTC/USD")
    assert asset.asset_class == "crypto"


# ------------------------------------------------------ Orderregeln
async def test_crypto_order_rules(paper_ctx: AppContext, fake):
    fake.set_quote("BTC/USD", 99.99, 100.01)
    fake.is_open = False  # Aktienmarkt zu – Krypto handelt trotzdem
    p = await paper_ctx.orders.preview("PAPER", crypto_buy(tif="day"))
    assert "Betrag/Gültigkeit" in failed(p)
    p = await paper_ctx.orders.preview("PAPER", crypto_buy())
    assert p["allowed"], failed(p)
    assert p["est_price"] == 100.01  # Kauf zum Briefkurs
    assert not any("geschlossen" in w for w in p["warnings"])
    p = await paper_ctx.orders.preview("PAPER", OrderInput(symbol="BTC/USD", side="buy", qty=Decimal("0.00001"), time_in_force="gtc"))
    assert "Menge/Gültigkeit" in failed(p)  # unter Mindestmenge


async def test_spread_filter_blocks_buys(paper_ctx: AppContext, fake):
    fake.set_quote("BTC/USD", 99.0, 101.0)  # 2 % Spread
    p = await paper_ctx.orders.preview("PAPER", crypto_buy())
    assert "Spread" in failed(p)


async def test_buys_per_hour_and_day(paper_ctx: AppContext, fake, clock):
    fake.set_quote("BTC/USD", 99.99, 100.01)
    set_risk_limits(paper_ctx.db, "PAPER", RiskLimits(max_buys_per_hour=2, max_total_invested=1000))
    for i in range(2):
        await paper_ctx.orders.submit("PAPER", crypto_buy("5"), source="manual", idempotency_key=f"h{i}")
    p = await paper_ctx.orders.preview("PAPER", crypto_buy("5"))
    assert "Einstiege pro Stunde" in failed(p)
    clock.advance(minutes=61)
    fake.set_quote("BTC/USD", 99.99, 100.01)
    p = await paper_ctx.orders.preview("PAPER", crypto_buy("5"))
    assert "Einstiege pro Stunde" not in failed(p)


async def test_loss_streak_pauses_buys_but_not_sells(paper_ctx: AppContext, fake, clock):
    db = paper_ctx.db
    set_risk_limits(db, "PAPER", RiskLimits(loss_streak_limit=3, loss_pause_min=30))
    for _ in range(3):
        db.execute("INSERT INTO trades (mode, symbol, status, net_pnl, created_at, updated_at) "
                   "VALUES ('PAPER', 'BTC/USD', 'closed', -0.4, ?, ?)", (clock().isoformat(), clock().isoformat()))
    last = db.query_one("SELECT MAX(id) AS id FROM trades")["id"]
    paper_ctx.bot.scalper._check_loss_streak("PAPER", last, clock())
    fake.set_quote("BTC/USD", 99.99, 100.01)
    assert "Pause nach Verlustserie" in failed(await paper_ctx.orders.preview("PAPER", crypto_buy()))
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    sell = await paper_ctx.orders.preview("PAPER", OrderInput(symbol="BTC/USD", side="sell", qty=Decimal("0.1"), time_in_force="gtc"))
    assert sell["allowed"], failed(sell)
    clock.advance(minutes=31)
    fake.set_quote("BTC/USD", 99.99, 100.01)
    assert "Pause nach Verlustserie" not in failed(await paper_ctx.orders.preview("PAPER", crypto_buy()))


# ------------------------------------------------------------ Scalping
async def start_scalper(ctx: AppContext, fake, mode: str = "PAPER") -> None:
    set_bot_params(ctx.db, mode, BotParams(**SCALP))
    fake.set_quote("BTC/USD", 99.99, 100.01)
    await ctx.bot.start(mode)
    ctx.bot._task.cancel()  # Durchläufe im Test von Hand takten


async def feed(ctx: AppContext, fake, clock, mids: list[float], spread: float = 0.02) -> None:
    """Pro Wert eine Mikrokerze (5 s): Kurs setzen, Zeit vorrücken, Durchlauf."""
    for mid in mids:
        clock.advance(seconds=5)
        fake.set_quote("BTC/USD", round(mid - spread / 2, 4), round(mid + spread / 2, 4))
        await ctx.bot.tick("PAPER")


async def test_scalp_start_requires_crypto_and_fresh_quote(paper_ctx: AppContext, fake, clock):
    with pytest.raises(ValueError):
        BotParams(**{**SCALP, "symbol": "SPY", "symbols": ["SPY"]})
    set_bot_params(paper_ctx.db, "PAPER", BotParams(**SCALP))
    fake.set_quote("BTC/USD", 99.99, 100.01, at=clock() - timedelta(minutes=5))
    with pytest.raises(BotStartError, match="Kein frischer"):
        await paper_ctx.bot.start("PAPER")
    fake.is_open = False  # Aktienbörse zu – für Krypto egal
    fake.set_quote("BTC/USD", 99.99, 100.01)
    await paper_ctx.bot.start("PAPER")
    assert paper_ctx.bot.running
    await paper_ctx.bot.pause()


async def test_scalp_full_trade_take_profit(paper_ctx: AppContext, fake, clock):
    await start_scalper(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, [100, 99.8, 99.6, 99.4, 99.2, 99.0])  # Aufwärmen, Abwärtstrend
    assert fake.count("POST", "/v2/orders") == 0
    await feed(paper_ctx, fake, clock, [99.9, 100.0])  # Kreuzung nach oben → Einstieg
    assert fake.count("POST", "/v2/orders") == 1
    entry = next(o for o in fake.orders.values() if o["side"] == "buy")
    assert entry["symbol"] == "BTC/USD" and entry["time_in_force"] == "gtc" and entry["notional"] == "20.00"
    # Weitere Durchläufe in derselben Lage → kein zweiter Einstieg
    await feed(paper_ctx, fake, clock, [100.0, 100.0])
    assert fake.count("POST", "/v2/orders") == 1
    fake.fill(entry, 0.2, 100.0)
    fake.add_position("BTCUSD", 0.1995, 100.0, asset_class="crypto")  # Gebühr mindert Bestand leicht
    await feed(paper_ctx, fake, clock, [100.2])
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["status"] == "open" and trade["stop_price"] == 99.5 and trade["target_price"] == 101.0
    await feed(paper_ctx, fake, clock, [101.2])  # Geldkurs 101.19 ≥ Ziel 101
    sells = [o for o in fake.orders.values() if o["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["qty"] == "0.1995"  # nur verfügbarer Bestand
    fake.fill(sells[0], 0.1995, 101.19)
    fake.positions.clear()
    await feed(paper_ctx, fake, clock, [101.0])
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["status"] == "closed" and trade["exit_reason"] == "Take-Profit"
    gross = (101.19 - 100.0) * 0.1995
    fees = 0.0025 * (100.0 * 0.2 + 101.19 * 0.1995)
    assert trade["gross_pnl"] == pytest.approx(gross, abs=1e-4)
    assert trade["net_pnl"] == pytest.approx(gross - fees, abs=1e-4)
    assert paper_ctx.bot.scalper.summary("PAPER")["trades_today"] == 1


async def test_scalp_stop_loss_and_max_hold(paper_ctx: AppContext, fake, clock):
    await start_scalper(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, [100, 99.8, 99.6, 99.4, 99.2, 99.0, 99.9, 100.0])
    entry = next(o for o in fake.orders.values() if o["side"] == "buy")
    fake.fill(entry, 0.2, 100.0)
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    await feed(paper_ctx, fake, clock, [99.6])  # über Stop (99.5) → bleibt offen
    assert paper_ctx.bot.scalper.list_trades("PAPER")[0]["status"] == "open"
    await feed(paper_ctx, fake, clock, [99.4])  # Geldkurs unter Stop → Verkauf
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["status"] == "exiting" and trade["exit_reason"] == "Stop-Loss"


async def test_scalp_time_exit(paper_ctx: AppContext, fake, clock):
    await start_scalper(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, [100, 99.8, 99.6, 99.4, 99.2, 99.0, 99.9, 100.0])
    entry = next(o for o in fake.orders.values() if o["side"] == "buy")
    fake.fill(entry, 0.2, 100.0)
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    await feed(paper_ctx, fake, clock, [100.1])
    clock.advance(minutes=2)  # über max. Haltedauer (1 Min.)
    await feed(paper_ctx, fake, clock, [100.1])
    assert paper_ctx.bot.scalper.list_trades("PAPER")[0]["exit_reason"] == "Max. Haltedauer"


async def test_paused_bot_sends_no_exit(paper_ctx: AppContext, fake, clock):
    await start_scalper(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, [100, 99.8, 99.6, 99.4, 99.2, 99.0, 99.9, 100.0])
    entry = next(o for o in fake.orders.values() if o["side"] == "buy")
    fake.fill(entry, 0.2, 100.0)
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    await paper_ctx.bot.scalper.finalize_all("PAPER")
    assert paper_ctx.bot.scalper.list_trades("PAPER")[0]["status"] == "open"
    await paper_ctx.bot.pause()
    fake.set_quote("BTC/USD", 90.0, 90.02)  # tief unter Stop
    await paper_ctx.bot.scalper.finalize_all("PAPER")  # Hintergrundabgleich: nur lesend
    assert fake.count("POST", "/v2/orders") == 1  # kein automatischer Verkauf bei Pause


async def test_stale_quote_prevents_entry(paper_ctx: AppContext, fake, clock):
    await start_scalper(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, [100, 99.8, 99.6, 99.4, 99.2, 99.0])
    clock.advance(seconds=5)
    fake.set_quote("BTC/USD", 99.89, 99.91, at=clock() - timedelta(seconds=60))
    for _ in range(3):
        clock.advance(seconds=5)
        await paper_ctx.bot.tick("PAPER")
    assert fake.count("POST", "/v2/orders") == 0


async def test_entry_blocked_by_spread_is_logged(paper_ctx: AppContext, fake, clock):
    await start_scalper(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, [100, 99.8, 99.6, 99.4, 99.2, 99.0])
    await feed(paper_ctx, fake, clock, [99.9, 100.0], spread=0.5)  # 0,5 % Spread
    assert fake.count("POST", "/v2/orders") == 0
    d = paper_ctx.db.list_decisions("PAPER")[0]
    assert d["action"] == "blocked" and "Spread" in d["reason"]


# ------------------------------------------------------------ Not-Aus
def test_emergency_stop_demo(settings, paper_broker, clock):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings, context_factory=lambda s, db: AppContext(s, db, paper_broker=paper_broker), background=False)
    H = {"X-Requested-With": "alpaca-bot-ui"}
    with TestClient(app) as c:
        r = c.post("/api/orders", headers=H, json={"expected_mode": "DEMO", "symbol": "BTC/USD", "side": "buy",
                                                   "notional": "30", "time_in_force": "gtc",
                                                   "idempotency_key": "kill-test-1", "confirm": True})
        assert r.status_code == 200, r.text
        import time

        time.sleep(1.2)  # DEMO füllt nach ~1 s
        c.post("/api/sync", headers=H)
        assert any(p["symbol"] == "BTC/USD" for p in c.get("/api/dashboard").json()["positions"])
        res = c.post("/api/emergency-stop", headers=H, json={"expected_mode": "DEMO"}).json()
        assert res["bot_paused"] and [x["symbol"] for x in res["closed"]] == ["BTC/USD"]
        time.sleep(1.2)
        c.post("/api/sync", headers=H)
        assert c.get("/api/dashboard").json()["positions"] == []


async def test_emergency_stop_paper_without_release_does_not_sell(paper_ctx: AppContext, fake):
    from fastapi.testclient import TestClient  # noqa: F401  (nur Logik, kein HTTP)

    from app.api import ModeOnly, emergency_stop

    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    open_order = fake.add_order(symbol="BTC/USD", side="buy", notional="5", status="new")
    paper_ctx.set_orders_enabled("PAPER", False)

    class Req:  # minimaler Request-Ersatz
        class app:  # noqa: N801
            class state:  # noqa: N801
                ctx = paper_ctx

    res = await emergency_stop(ModeOnly(expected_mode="PAPER"), Req())  # type: ignore[arg-type]
    assert fake.orders[open_order["id"]]["status"] == "canceled"
    assert res["not_closed"][0]["reason"] == "Paper-Orderfreigabe ist aus"
    assert fake.count("POST", "/v2/orders") == 0


async def test_crypto_close_position_route_accepts_slash(paper_ctx: AppContext, fake):
    fake.set_quote("BTC/USD", 99.99, 100.01)
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    res = await paper_ctx.orders.close_position("PAPER", "BTC/USD", "close-btc-1")
    sell = next(o for o in fake.orders.values() if o["side"] == "sell")
    assert sell["symbol"] == "BTC/USD" and sell["time_in_force"] == "gtc" and sell["qty"] == "0.2"
    assert res["order"]["status"] == "accepted"
    assert httpx  # noqa: B018
