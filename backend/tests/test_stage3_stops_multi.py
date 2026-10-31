"""Stufe 3: Schutzorder beim Broker, mehrere Symbole (Mock-Tests, keine echten Orders)."""

from __future__ import annotations

from decimal import Decimal


from app.broker.demo import DemoBroker, demo_quote_prices
from app.broker.base import OrderRequest
from app.core import AppContext
from app.orders import OrderInput
from app.scalper import STOP_GRACE_S, round_down_price
from app.settings_store import BotParams, RiskLimits, set_bot_params, set_risk_limits

BASE = dict(strategy="scalp", atr_exits=False, trend_filter=False, symbol="BTC/USD", symbols=["BTC/USD"], short_window=2, long_window=4, bar_seconds=5,
            poll_interval_s=1, order_value=20, stop_loss_pct=0.5, take_profit_pct=1.0, max_hold_min=10, fee_pct=0.25,
            broker_stop=True, stop_limit_offset_pct=0.5)
DOWN_UP = [100, 99.8, 99.6, 99.4, 99.2, 99.0, 99.9, 100.0]


def failed(preview: dict) -> dict[str, str]:
    return {c["name"]: c["detail"] for c in preview["checks"] if c["blocking"] and not c["ok"]}


async def start(ctx: AppContext, fake, **kw) -> None:
    params = BotParams(**{**BASE, **kw})
    set_bot_params(ctx.db, "PAPER", params)
    for s in params.scalp_symbols:
        fake.set_quote(s, 99.99, 100.01)
    await ctx.bot.start("PAPER")
    ctx.bot._task.cancel()


async def feed(ctx, fake, clock, mids, symbols=("BTC/USD",), spread=0.02):
    for mid in mids:
        clock.advance(seconds=5)
        for s in symbols:
            fake.set_quote(s, round(mid - spread / 2, 4), round(mid + spread / 2, 4))
        await ctx.bot.tick("PAPER")


def orders_of(fake, **match):
    return [o for o in fake.orders.values() if all(o.get(k) == v for k, v in match.items())]


async def open_trade_with_stop(ctx, fake, clock):
    await start(ctx, fake)
    await feed(ctx, fake, clock, DOWN_UP)
    entry = orders_of(fake, side="buy")[0]
    fake.fill(entry, 0.2, 100.0)
    fake.add_position("BTCUSD", 0.1995, 100.0, asset_class="crypto")
    await feed(ctx, fake, clock, [100.1])  # → offen + Schutzorder
    return entry


# ------------------------------------------------------------ Hilfen
def test_round_down_price():
    assert round_down_price(99.4999, 0.01) == Decimal("99.49")
    assert round_down_price(67123.7, 1) == Decimal("67123")
    assert round_down_price(0.123456789, None) == Decimal("0.123456")
    assert round_down_price(12.345, None) == Decimal("12.34")


# ------------------------------------------------------------ Orderweg
async def test_stop_limit_rules_and_manual_market_only(paper_ctx: AppContext, fake):
    fake.set_quote("BTC/USD", 99.99, 100.01)
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    ok = OrderInput(symbol="BTC/USD", side="sell", qty=Decimal("0.1"), time_in_force="gtc", type="stop_limit",
                    stop_price=Decimal("99"), limit_price=Decimal("98.5"))
    assert (await paper_ctx.orders.preview("PAPER", ok))["allowed"]
    bad_limit = ok.model_copy(update={"limit_price": Decimal("99.5")})
    assert "Schutzorder" in failed(await paper_ctx.orders.preview("PAPER", bad_limit))
    bad_side = OrderInput(symbol="BTC/USD", side="buy", qty=Decimal("0.1"), time_in_force="gtc", type="stop_limit",
                          stop_price=Decimal("99"), limit_price=Decimal("98"))
    assert "Schutzorder" in failed(await paper_ctx.orders.preview("PAPER", bad_side))


def test_api_rejects_manual_stop_orders(settings, paper_broker):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings, context_factory=lambda s, db: AppContext(s, db, paper_broker=paper_broker), background=False)
    with TestClient(app) as c:
        r = c.post("/api/orders/preview", headers={"X-Requested-With": "alpaca-bot-ui"},
                   json={"expected_mode": "DEMO", "symbol": "BTC/USD", "side": "sell", "qty": "0.1", "time_in_force": "gtc",
                         "type": "stop_limit", "stop_price": "1", "limit_price": "1"})
        assert r.status_code == 400


# ------------------------------------------------------------ Schutzorder
async def test_stop_placed_after_fill(paper_ctx: AppContext, fake, clock):
    await open_trade_with_stop(paper_ctx, fake, clock)
    stops = orders_of(fake, type="stop_limit")
    assert len(stops) == 1
    s = stops[0]
    assert s["side"] == "sell" and s["time_in_force"] == "gtc" and s["qty"] == "0.1995"  # verfügbarer Bestand
    assert s["stop_price"] == "99.5" and s["limit_price"] == "99"  # 100 − 0,5 %; Limit 0,5 % darunter
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["status"] == "open" and trade["stop_cid"] == s["client_order_id"]


async def test_take_profit_cancels_stop_then_sells(paper_ctx: AppContext, fake, clock):
    await open_trade_with_stop(paper_ctx, fake, clock)
    stop = orders_of(fake, type="stop_limit")[0]
    await feed(paper_ctx, fake, clock, [101.2])  # Ziel 101 erreicht
    assert fake.count("DELETE", f"/v2/orders/{stop['id']}") == 1
    assert stop["status"] == "canceled"
    sells = orders_of(fake, side="sell", type="market")
    assert len(sells) == 1 and sells[0]["qty"] == "0.1995"
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["status"] == "exiting" and trade["exit_reason"] == "Take-Profit"


async def test_stop_loss_handled_by_broker_stop(paper_ctx: AppContext, fake, clock):
    await open_trade_with_stop(paper_ctx, fake, clock)
    stop = orders_of(fake, type="stop_limit")[0]
    await feed(paper_ctx, fake, clock, [99.3])  # unter Stop
    assert not orders_of(fake, side="sell", type="market")  # kein zusätzlicher Bot-Verkauf
    fake.fill(stop, 0.1995, 99.4)
    fake.positions.clear()
    await feed(paper_ctx, fake, clock, [99.3])
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["status"] == "closed" and trade["exit_reason"] == "Stop-Loss (Schutzorder)"
    assert trade["exit_price"] == 99.4 and trade["net_pnl"] < 0


async def test_stop_loss_fallback_when_broker_stop_does_not_fill(paper_ctx: AppContext, fake, clock):
    await open_trade_with_stop(paper_ctx, fake, clock)
    stop = orders_of(fake, type="stop_limit")[0]
    await feed(paper_ctx, fake, clock, [98.0])  # Kurssturz unter das Limit → Stop füllt nicht
    clock.advance(seconds=STOP_GRACE_S + 1)
    await feed(paper_ctx, fake, clock, [98.0])
    assert stop["status"] == "canceled"
    sells = orders_of(fake, side="sell", type="market")
    assert len(sells) == 1
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["exit_reason"] == "Stop-Loss (Schutzorder nicht ausgeführt)"


async def test_paused_bot_records_broker_stop_fill_without_orders(paper_ctx: AppContext, fake, clock):
    await open_trade_with_stop(paper_ctx, fake, clock)
    await paper_ctx.bot.pause()
    posts = fake.count("POST", "/v2/orders")
    stop = orders_of(fake, type="stop_limit")[0]
    assert stop["status"] == "new"  # Schutzorder bleibt bei Pause aktiv
    fake.fill(stop, 0.1995, 99.45)
    fake.positions.clear()
    await paper_ctx.bot.scalper.finalize_all("PAPER")
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["status"] == "closed" and trade["exit_reason"] == "Stop-Loss (Schutzorder)"
    assert fake.count("POST", "/v2/orders") == posts


async def test_close_position_cancels_protective_stop(paper_ctx: AppContext, fake):
    fake.set_quote("BTC/USD", 99.99, 100.01)
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    stop = fake.add_order(symbol="BTC/USD", side="sell", qty="0.2", status="new")
    stop.update(type="stop_limit", stop_price="95", limit_price="94")
    await paper_ctx.orders.close_position("PAPER", "BTC/USD", "close-with-stop")
    assert stop["status"] == "canceled"
    assert len(orders_of(fake, side="sell", type="market", symbol="BTC/USD")) == 1


# ------------------------------------------------------------ Mehrere Symbole
async def test_multiple_symbols_enter_independently(paper_ctx: AppContext, fake, clock):
    set_risk_limits(paper_ctx.db, "PAPER", RiskLimits(max_buys_per_hour=10))
    await start(paper_ctx, fake, symbols=["BTC/USD", "ETH/USD"], broker_stop=False)
    await feed(paper_ctx, fake, clock, DOWN_UP, symbols=("BTC/USD", "ETH/USD"))
    buys = orders_of(fake, side="buy")
    assert sorted(o["symbol"] for o in buys) == ["BTC/USD", "ETH/USD"]
    state = paper_ctx.bot.scalper.state
    assert set(state["symbols"]) == {"BTC/USD", "ETH/USD"}
    # Ein Abruf für beide Kurse je Durchlauf
    quote_calls = fake.count("GET", "/v1beta3/crypto/us/latest/quotes")
    assert quote_calls <= len(DOWN_UP) + 6


async def test_shared_risk_limits_across_symbols(paper_ctx: AppContext, fake, clock):
    set_risk_limits(paper_ctx.db, "PAPER", RiskLimits(max_open_positions=1))
    await start(paper_ctx, fake, symbols=["BTC/USD", "ETH/USD"], broker_stop=False)
    fake.add_position("SPY", 1, 10.0)  # bereits eine Position offen
    await feed(paper_ctx, fake, clock, DOWN_UP, symbols=("BTC/USD", "ETH/USD"))
    assert not orders_of(fake, side="buy")
    blocked = [d for d in paper_ctx.db.list_decisions("PAPER") if d["action"] == "blocked"]
    assert {d["symbol"] for d in blocked} == {"BTC/USD", "ETH/USD"} and "Offene Positionen" in blocked[0]["reason"]


# ------------------------------------------------------------ DEMO
async def test_demo_stop_limit_simulation(db, clock):
    broker = DemoBroker(db, now=clock)
    await broker.submit_order(OrderRequest(symbol="BTC/USD", side="buy", notional=Decimal("100"), time_in_force="gtc", client_order_id="d-buy"))
    clock.advance(seconds=2)
    pos = (await broker.list_positions())[0]
    bid, _ = demo_quote_prices("BTC/USD", clock().timestamp())
    stop = await broker.submit_order(OrderRequest(symbol="BTC/USD", side="sell", qty=Decimal(str(pos.qty)), time_in_force="gtc",
                                                  client_order_id="d-stop", type="stop_limit",
                                                  stop_price=Decimal(str(bid * 0.999)), limit_price=Decimal(str(bid * 0.99))))
    assert (await broker.list_positions())[0].qty_available == 0  # Bestand durch Stop gebunden
    for _ in range(240):  # bis der Demo-Kurs den Stop berührt
        clock.advance(seconds=15)
        o = await broker.get_order(stop.id)
        if o.status == "filled":
            break
    assert o.status == "filled" and o.filled_avg_price <= bid * 0.999 + 1e-6
    assert await broker.list_positions() == []
