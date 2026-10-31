"""Scalping mit US-ETFs (Handelszeit, Glattstellen vor Schluss) und Trendfilter – nur Mocks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest import minutes_to_close, simulate
from app.core import AppContext
from app.settings_store import BotParams, RiskLimits, set_bot_params, set_risk_limits
from test_backtest_symbols import req, series

BASE = dict(strategy="scalp", atr_exits=False, symbol="SPY", symbols=["SPY"], short_window=2, long_window=4, bar_seconds=5,
            poll_interval_s=1, order_value=20, trend_filter=False, broker_stop=True,
            stock_stop_loss_pct=1.0, stock_take_profit_pct=1.0, stock_max_hold_min=30)
DOWN_UP = [100, 99.8, 99.6, 99.4, 99.2, 99.0, 99.9, 100.0]
UTC = timezone.utc
QUOTES = "/v2/stocks/quotes/latest"


async def start(ctx: AppContext, fake, **kw) -> BotParams:
    params = BotParams(**{**BASE, **kw})
    set_bot_params(ctx.db, "PAPER", params)
    fake.prices["SPY"] = 100.0
    for s in params.scalp_symbols:
        (fake.set_quote if "/" in s else fake.set_stock_quote)(s, 99.99, 100.01)
    await ctx.bot.start("PAPER")
    ctx.bot._task.cancel()
    return params


async def feed(ctx, fake, clock, mids, symbols=("SPY",), spread=0.02):
    for mid in mids:
        clock.advance(seconds=5)
        for s in symbols:
            (fake.set_quote if "/" in s else fake.set_stock_quote)(s, round(mid - spread / 2, 4), round(mid + spread / 2, 4))
        await ctx.bot.tick("PAPER")


def orders(fake, **match):
    return [o for o in fake.orders.values() if all(o.get(k) == v for k, v in match.items())]


def decisions(ctx, action=None):
    return [d for d in ctx.db.list_decisions("PAPER", 100) if action is None or d["action"] == action]


# ------------------------------------------------------------ Alpaca-Adapter
async def test_stock_quotes_in_one_request(paper_broker, fake):
    fake.set_stock_quote("SPY", 99.99, 100.01)
    fake.set_stock_quote("QQQ", 449.9, 450.1)
    fake.set_quote("BTC/USD", 67000, 67010)
    quotes = await paper_broker.get_quotes(["SPY", "QQQ", "BTC/USD"])
    assert set(quotes) == {"SPY", "QQQ", "BTC/USD"} and quotes["QQQ"].ask == 450.1
    assert fake.count("GET", QUOTES) == 1 and "IEX" in quotes["SPY"].source


# ------------------------------------------------------------ ETF-Scalping
async def test_etf_trade_uses_day_orders_and_no_broker_stop(paper_ctx: AppContext, fake, clock):
    await start(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, DOWN_UP)
    buy = orders(fake, side="buy")[0]
    assert buy["symbol"] == "SPY" and buy["notional"] == "20.00" and buy["time_in_force"] == "day"
    fake.fill(buy, 0.2, 100.0)
    fake.add_position("SPY", 0.2, 100.0)
    await feed(paper_ctx, fake, clock, [100.1])
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["status"] == "open" and trade["fee_pct"] == 0.0  # kommissionsfrei
    assert trade["target_price"] == 101.0 and trade["stop_price"] == 99.0  # Aktien-Profil (je 1 %)
    assert not orders(fake, type="stop_limit")  # keine Schutzorder für Aktien/ETFs
    await feed(paper_ctx, fake, clock, [101.2])
    sell = orders(fake, side="sell")[0]
    assert sell["time_in_force"] == "day" and sell["qty"] == "0.2"
    assert paper_ctx.bot.scalper.list_trades("PAPER")[0]["exit_reason"] == "Take-Profit"


async def test_etf_waits_while_market_closed(paper_ctx: AppContext, fake, clock):
    fake.is_open = False
    await paper_ctx.runtime.sync()
    await start(paper_ctx, fake)  # Start ohne frischen Aktienkurs erlaubt – wartet auf Eröffnung
    polls = fake.count("GET", QUOTES)
    await feed(paper_ctx, fake, clock, DOWN_UP)
    assert fake.count("GET", QUOTES) == polls and not orders(fake, side="buy")
    view = paper_ctx.bot.scalper.state["symbols"]["SPY"]
    assert view["status"].startswith("US-Börse geschlossen")


async def test_no_new_etf_entries_shortly_before_close(paper_ctx: AppContext, fake, clock):
    clock.t = datetime(2026, 10, 30, 19, 47, tzinfo=UTC)  # 15:47 New York, Schluss 16:00
    await start(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, DOWN_UP)
    assert not orders(fake, side="buy")
    skipped = decisions(paper_ctx, "skip")
    assert skipped and "Kurz vor Börsenschluss" in skipped[0]["reason"]


async def test_open_etf_trade_is_closed_before_the_bell(paper_ctx: AppContext, fake, clock):
    clock.t = datetime(2026, 10, 30, 19, 40, tzinfo=UTC)  # 15:40 New York
    await start(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, DOWN_UP)
    buy = orders(fake, side="buy")[0]
    fake.fill(buy, 0.2, 100.0)
    fake.add_position("SPY", 0.2, 100.0)
    await feed(paper_ctx, fake, clock, [100.1])
    assert paper_ctx.bot.scalper.list_trades("PAPER")[0]["status"] == "open"
    clock.t = datetime(2026, 10, 30, 19, 55, 30, tzinfo=UTC)  # 4,5 Min. vor Schluss
    await feed(paper_ctx, fake, clock, [100.1])
    assert orders(fake, side="sell") and paper_ctx.bot.scalper.list_trades("PAPER")[0]["exit_reason"] == "Börsenschluss"


async def test_crypto_pauses_while_us_session_is_open(paper_ctx: AppContext, fake, clock):
    set_risk_limits(paper_ctx.db, "PAPER", RiskLimits(max_open_positions=5))
    await start(paper_ctx, fake, symbol="BTC/USD", symbols=["SPY", "BTC/USD"])
    await feed(paper_ctx, fake, clock, DOWN_UP, symbols=("BTC/USD",))
    assert not orders(fake, symbol="BTC/USD")
    assert any("Krypto pausiert" in d["reason"] for d in decisions(paper_ctx, "skip"))
    # Börse zu → Krypto handelt wieder
    fake.is_open = False
    await paper_ctx.runtime.sync()
    await feed(paper_ctx, fake, clock, [99.0, 98.8, 98.6, 98.4, 98.2, 98.0, 98.9, 99.0], symbols=("BTC/USD",))
    assert orders(fake, symbol="BTC/USD", side="buy")


async def test_crypto_can_trade_during_session_if_wanted(paper_ctx: AppContext, fake, clock):
    set_risk_limits(paper_ctx.db, "PAPER", RiskLimits(max_open_positions=5))
    await start(paper_ctx, fake, symbol="BTC/USD", symbols=["SPY", "BTC/USD"], crypto_outside_session_only=False)
    await feed(paper_ctx, fake, clock, DOWN_UP, symbols=("BTC/USD",))
    assert orders(fake, symbol="BTC/USD", side="buy")


async def test_wide_iex_spread_blocks_etf_entry(paper_ctx: AppContext, fake, clock):
    await start(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, DOWN_UP, spread=0.5)  # 0,5 % > Grenze 0,1 %
    assert not orders(fake, side="buy")
    assert "Spread" in decisions(paper_ctx, "blocked")[0]["reason"]


async def test_etf_must_be_fractionable(paper_ctx: AppContext, fake):
    from app.bot import BotStartError

    set_bot_params(paper_ctx.db, "PAPER", BotParams(**{**BASE, "symbol": "BRK.A", "symbols": ["BRK.A"]}))
    try:
        await paper_ctx.bot.start("PAPER")
    except BotStartError as exc:
        assert "bruchstückfähig" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Start hätte abgelehnt werden müssen")


# ------------------------------------------------------------ Trendfilter (Bot)
async def test_trend_filter_blocks_falling_and_allows_rising(paper_ctx: AppContext, fake, clock):
    params = dict(symbol="BTC/USD", symbols=["BTC/USD"], trend_filter=True, trend_minutes=10)
    fake.make_bars("BTC/USD", [120 - i for i in range(16)])  # fallend 120 → 105
    await start(paper_ctx, fake, **params)
    await feed(paper_ctx, fake, clock, DOWN_UP, symbols=("BTC/USD",))
    assert not orders(fake, side="buy")
    skip = [d for d in decisions(paper_ctx, "skip") if "Trendfilter" in d["reason"]]
    assert skip and "nicht über dem 10-Min.-Durchschnitt" in skip[0]["reason"]

    paper_ctx.market.clear()
    fake.make_bars("BTC/USD", [90 + i * 0.6 for i in range(16)])  # steigend 90 → 99
    await feed(paper_ctx, fake, clock, [99.0, 98.8, 98.6, 98.4, 98.2, 98.0, 99.9, 100.0], symbols=("BTC/USD",))
    assert orders(fake, side="buy", symbol="BTC/USD")


async def test_trend_filter_needs_enough_bars(paper_ctx: AppContext, fake, clock):
    fake.make_bars("BTC/USD", [100.0] * 5)
    await start(paper_ctx, fake, symbol="BTC/USD", symbols=["BTC/USD"], trend_filter=True, trend_minutes=10)
    await feed(paper_ctx, fake, clock, DOWN_UP, symbols=("BTC/USD",))
    assert not orders(fake, side="buy")
    assert any("zu wenige Minutenkerzen" in d["reason"] for d in decisions(paper_ctx, "skip"))


# ------------------------------------------------------------ Backtest
NY_1530 = datetime(2026, 10, 30, 19, 30, tzinfo=UTC)  # Freitag 15:30 New York (EDT)


def test_minutes_to_close():
    assert minutes_to_close(NY_1530) == 30
    assert minutes_to_close(datetime(2026, 11, 2, 20, 50, tzinfo=UTC)) == 10  # nach Zeitumstellung (EST)


def test_backtest_etf_scalp_flattens_before_close():
    closes = [100, 99, 98, 97, 98, 99, 100] + [100.0] * 23
    r = simulate(series(closes, start=NY_1530), req(symbol="SPY", fee_pct=0), None, session_reset=True)
    assert r["metrics"]["trades"] == 1
    t = r["trades"][0]
    assert t["reason"] == "Börsenschluss" and datetime.fromisoformat(t["exit_time"]) <= datetime(2026, 10, 30, 19, 56, tzinfo=UTC)
    assert t["fees"] == 0


def test_backtest_no_etf_entry_in_last_minutes():
    closes = [100, 99, 98, 97, 98, 99, 100, 100, 100]
    r = simulate(series(closes, start=NY_1530 + timedelta(minutes=10)), req(symbol="SPY"), None, session_reset=True)
    assert r["metrics"]["trades"] == 0 and r["metrics"]["blocked_entries"].get("Kurz vor Börsenschluss", 0) >= 1


def test_backtest_trend_filter_skips_bounces_in_downtrend():
    closes: list[float] = []
    for k in range(12):
        base = 200 - k * 4
        closes += [base, base - 1, base - 2, base - 3, base - 2, base - 1]
    bars = series(closes)
    without = simulate(bars, req(), None, session_reset=False)
    with_filter = simulate(bars, req(trend_filter=True, trend_minutes=10), None, session_reset=False)
    assert without["metrics"]["trades"] > 0
    assert with_filter["metrics"]["trades"] == 0 and with_filter["metrics"]["blocked_entries"]["Trendfilter"] > 0


def test_backtest_trend_filter_allows_uptrend():
    closes: list[float] = []
    for k in range(12):
        base = 100 + k * 4
        closes += [base, base - 1, base - 2, base - 1, base + 1, base + 2]
    r = simulate(series(closes), req(trend_filter=True, trend_minutes=10), None, session_reset=False)
    assert r["metrics"]["trades"] > 0
