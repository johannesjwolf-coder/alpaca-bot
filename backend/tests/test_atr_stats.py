"""ATR-Ausstiege und Statistik je Symbol mit Auto-Pause (nur Mocks)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.backtest import simulate
from app.broker.base import Bar
from app.core import AppContext
from app.main import create_app
from app.settings_store import BotParams, set_bot_params
from app.strategy import atr_exit_pcts, atr_pct
from test_backtest_symbols import req, series

UTC = timezone.utc
UI = {"X-Requested-With": "alpaca-bot-ui"}
SCALP = dict(strategy="scalp", symbol="BTC/USD", symbols=["BTC/USD"], short_window=2, long_window=4, bar_seconds=5,
             poll_interval_s=1, order_value=20, broker_stop=False, trend_filter=False,
             atr_exits=True, atr_timeframe="15Min", atr_periods=3, atr_stop_mult=1.5, atr_target_mult=3.0,
             stop_loss_pct=1.0, take_profit_pct=2.0)
DOWN_UP = [100, 99.8, 99.6, 99.4, 99.2, 99.0, 99.9, 100.0]


def bar(t: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(t=t, o=o, h=h, l=low, c=c)


# ------------------------------------------------------------ Berechnung
def test_atr_pct_true_range_and_gaps():
    t0 = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
    step = timedelta(minutes=15)
    bars = [bar(t0 + i * step, 100, 100.5, 99.5, 100) for i in range(4)]  # Spanne je 1,0
    assert atr_pct(bars, 3, 900) == 1.0
    # Kurssprung ohne Lücke zählt (|Hoch − voriger Schluss|) ...
    jump = [*bars, bar(t0 + 4 * step, 102, 102.5, 101.5, 102)]
    assert round(atr_pct(jump, 3, 900), 4) == round((1 + 1 + 2.5) / 3 / 102 * 100, 4)
    # ... nach einer Lücke (z. B. Börsenschluss) nur die Spanne der Kerze
    gap = [*bars, bar(t0 + 20 * step, 102, 102.5, 101.5, 102)]
    assert round(atr_pct(gap, 3, 900), 4) == round(1 / 102 * 100, 4)
    assert atr_pct(bars[:3], 3, 900) is None  # zu wenige Kerzen


def test_atr_exit_limits_and_cost_floor():
    assert atr_exit_pcts(1.0, 1.5, 3.0, 0.25) == (1.5, 3.0)
    assert atr_exit_pcts(0.05, 1.5, 3.0, 0.25) == (0.075, 0.6)  # Ziel mind. Kosten 0,5 % + 0,1 %
    assert atr_exit_pcts(0.001, 1.0, 1.0, 0.0) == (0.05, 0.1)  # Untergrenzen
    assert atr_exit_pcts(20, 1.0, 2.0, 0.0) == (10.0, 20.0)  # Obergrenzen


# ------------------------------------------------------------ Bot
async def start(ctx, fake, **kw):
    set_bot_params(ctx.db, "PAPER", BotParams(**{**SCALP, **kw}))
    fake.set_quote("BTC/USD", 99.99, 100.01)
    await ctx.bot.start("PAPER")
    ctx.bot._task.cancel()


async def feed(ctx, fake, clock, mids):
    for mid in mids:
        clock.advance(seconds=5)
        fake.set_quote("BTC/USD", round(mid - 0.01, 4), round(mid + 0.01, 4))
        await ctx.bot.tick("PAPER")


def atr_bars(fake, clock, n: int = 5, spread: float = 1.0) -> None:
    """Abgeschlossene 15-Min.-Kerzen mit Spanne ``spread`` um 100 (ATR = spread %)."""
    end = datetime.fromtimestamp(clock().timestamp() // 900 * 900, tz=UTC) - timedelta(minutes=15)
    fake.bars["BTC/USD"] = [
        {"t": (end - timedelta(minutes=15 * (n - 1 - i))).isoformat().replace("+00:00", "Z"),
         "o": 100, "h": 100 + spread / 2, "l": 100 - spread / 2, "c": 100, "v": 1}
        for i in range(n)
    ]


async def test_bot_sets_exits_from_atr(paper_ctx: AppContext, fake, clock):
    atr_bars(fake, clock)
    await start(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, DOWN_UP)
    buy = [o for o in fake.orders.values() if o["side"] == "buy"][0]
    assert any("ATR 1.000 %" in d["reason"] for d in paper_ctx.db.list_decisions("PAPER", 20))
    fake.fill(buy, 0.2, 100.0)
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    await feed(paper_ctx, fake, clock, [100.1])
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["atr_pct"] == 1.0
    assert trade["stop_price"] == 98.5 and trade["target_price"] == 103.0  # 1,5 × / 3 × ATR
    view = paper_ctx.bot.scalper.state["symbols"]["BTC/USD"]["trade"]
    assert view["atr_pct"] == 1.0


async def test_bot_falls_back_to_fixed_exits_without_bars(paper_ctx: AppContext, fake, clock):
    await start(paper_ctx, fake)
    await feed(paper_ctx, fake, clock, DOWN_UP)
    buy = [o for o in fake.orders.values() if o["side"] == "buy"][0]
    assert any("ATR nicht bestimmbar" in d["reason"] for d in paper_ctx.db.list_decisions("PAPER", 20))
    fake.fill(buy, 0.2, 100.0)
    fake.add_position("BTCUSD", 0.2, 100.0, asset_class="crypto")
    await feed(paper_ctx, fake, clock, [100.1])
    trade = paper_ctx.bot.scalper.list_trades("PAPER")[0]
    assert trade["atr_pct"] is None and trade["stop_price"] == 99.0 and trade["target_price"] == 102.0


# ------------------------------------------------------------ Statistik & Auto-Pause
def add_closed(db, symbol: str, nets: list[float], exit_time: datetime, mode: str = "PAPER") -> int:
    last = 0
    for i, net in enumerate(nets):
        t = exit_time + timedelta(minutes=i)
        db.execute(
            "INSERT INTO trades (mode, symbol, status, entry_time, exit_time, exit_reason, net_pnl, created_at, updated_at) "
            "VALUES (?, ?, 'closed', ?, ?, ?, ?, ?, ?)",
            (mode, symbol, (t - timedelta(minutes=10)).isoformat(), t.isoformat(),
             "Take-Profit" if net > 0 else "Stop-Loss", net, t.isoformat(), t.isoformat()),
        )
        last = db.query_one("SELECT max(id) AS id FROM trades")["id"]
    return last


async def test_symbol_stats(paper_ctx: AppContext, clock):
    now = clock()
    add_closed(paper_ctx.db, "BTC/USD", [1.0, -0.5, 2.0], now - timedelta(days=1))
    add_closed(paper_ctx.db, "SPY", [-0.2, -0.3], now - timedelta(days=1))
    add_closed(paper_ctx.db, "BTC/USD", [-5.0], now - timedelta(days=40))  # ausserhalb von 30 Tagen
    stats = {s["symbol"]: s for s in paper_ctx.bot.scalper.symbol_stats("PAPER", 30, ["BTC/USD", "ETH/USD", "SPY"])}
    btc = stats["BTC/USD"]
    assert btc["trades"] == 3 and btc["wins"] == 2 and btc["win_rate_pct"] == 66.7
    assert btc["net"] == 2.5 and btc["profit_factor"] == 6.0 and btc["avg_hold_min"] == 10.0
    assert btc["exit_reasons"] == {"Take-Profit": 2, "Stop-Loss": 1}
    assert stats["ETH/USD"]["trades"] == 0 and stats["ETH/USD"]["win_rate_pct"] is None
    assert stats["SPY"]["net"] == -0.5
    all_time = {s["symbol"]: s for s in paper_ctx.bot.scalper.symbol_stats("PAPER", None, ["BTC/USD"])}
    assert all_time["BTC/USD"]["trades"] == 4 and all_time["SPY"]["active"] is False


async def test_symbol_auto_pause_blocks_only_that_symbol(paper_ctx: AppContext, fake, clock):
    set_bot_params(paper_ctx.db, "PAPER", BotParams(**{**SCALP, "atr_exits": False, "symbol_pause_trades": 3}))
    eng = paper_ctx.bot.scalper
    last = add_closed(paper_ctx.db, "BTC/USD", [0.5, -0.4, -0.3], clock() - timedelta(hours=1))
    eng._check_symbol_pause("PAPER", "BTC/USD", last, clock())
    pause = eng.symbol_pause("PAPER", "BTC/USD", clock())
    assert pause and "letzte 3 Trades zusammen -0.20 USD" in pause["reason"]
    assert eng.symbol_pause("PAPER", "ETH/USD", clock()) is None
    assert any("BTC/USD pausiert" in e["message"] for e in paper_ctx.db.list_events("PAPER", 10))
    # Kaufsignal wird ausgelassen
    await start(paper_ctx, fake, atr_exits=False, symbol_pause_trades=3)
    await feed(paper_ctx, fake, clock, DOWN_UP)
    assert not [o for o in fake.orders.values() if o["side"] == "buy"]
    assert any("Symbol pausiert" in d["reason"] for d in paper_ctx.db.list_decisions("PAPER", 20))
    # Nach Ablauf wieder frei; neues Bewertungsfenster (alte Verluste zählen nicht erneut)
    clock.advance(hours=25)
    assert eng.symbol_pause("PAPER", "BTC/USD", clock()) is None
    last = add_closed(paper_ctx.db, "BTC/USD", [-0.1], clock())
    eng._check_symbol_pause("PAPER", "BTC/USD", last, clock())
    assert eng.symbol_pause("PAPER", "BTC/USD", clock()) is None


async def test_symbol_pause_needs_enough_trades_and_a_net_loss(paper_ctx: AppContext, clock):
    set_bot_params(paper_ctx.db, "PAPER", BotParams(**{**SCALP, "symbol_pause_trades": 3}))
    eng = paper_ctx.bot.scalper
    last = add_closed(paper_ctx.db, "BTC/USD", [-1.0, -1.0], clock())
    eng._check_symbol_pause("PAPER", "BTC/USD", last, clock())
    assert eng.symbol_pause("PAPER", "BTC/USD", clock()) is None  # erst 2 von 3 Trades
    last = add_closed(paper_ctx.db, "BTC/USD", [2.5], clock())
    eng._check_symbol_pause("PAPER", "BTC/USD", last, clock())
    assert eng.symbol_pause("PAPER", "BTC/USD", clock()) is None  # zusammen im Plus
    set_bot_params(paper_ctx.db, "PAPER", BotParams(**{**SCALP, "symbol_pause": False, "symbol_pause_trades": 3}))
    last = add_closed(paper_ctx.db, "BTC/USD", [-3.0, -3.0, -3.0], clock())
    eng._check_symbol_pause("PAPER", "BTC/USD", last, clock())
    assert eng.symbol_pause("PAPER", "BTC/USD", clock()) is None  # Auto-Pause ausgeschaltet


def test_stats_and_resume_api(settings, paper_broker):
    app = create_app(settings, context_factory=lambda s, db: AppContext(s, db, paper_broker=paper_broker), background=False)
    with TestClient(app) as c:
        ctx: AppContext = app.state.ctx
        r = c.get("/api/bot/stats?days=0").json()
        assert r["days"] is None and r["pause_rule"] == {"enabled": True, "trades": 10, "hours": 24.0}
        assert {s["symbol"] for s in r["symbols"]} >= {"BTC/USD", "ETH/USD", "SOL/USD"}
        body = {"expected_mode": "DEMO", "symbol": "BTC/USD"}
        assert c.post("/api/bot/symbols/resume", json=body).status_code == 403  # ohne CSRF-Kopf
        assert c.post("/api/bot/symbols/resume", headers=UI, json=body).status_code == 409  # nicht pausiert
        ctx.db.set("DEMO", "symbol_pause:BTC/USD", {"until": "2099-01-01T00:00:00+00:00", "reason": "Test"})
        assert c.get("/api/bot/stats").json()["symbols"][0]["paused_until"] is not None
        assert c.post("/api/bot/symbols/resume", headers=UI, json=body).json()["resumed"] is True
        assert ctx.bot.scalper.symbol_pause("DEMO", "BTC/USD", ctx._now()) is None


# ------------------------------------------------------------ Backtest
def test_backtest_atr_exits():
    closes: list[float] = []
    for k in range(30):  # 180 Minuten Auf und Ab → genügend 15-Min.-Kerzen für ATR(3)
        closes += [100, 99, 98, 97, 98, 99]
    bars = series(closes, hl=0.5)
    fixed = simulate(bars, req(stop_loss_pct=1.0, take_profit_pct=2.0), None, session_reset=False)
    atr = simulate(bars, req(stop_loss_pct=1.0, take_profit_pct=2.0, atr_exits=True, atr_periods=3), None, session_reset=False)
    assert fixed["metrics"]["atr_trades"] == 0
    m = atr["metrics"]
    assert 0 < m["atr_trades"] < m["trades"]  # anfangs feste Werte, danach ATR
    first_atr = next(t for t in atr["trades"] if t["atr_pct"])
    assert first_atr["stop_pct"] == round(min(max(first_atr["atr_pct"] * 1.5, 0.05), 10), 4)
    early = atr["trades"][0]
    assert early["atr_pct"] is None and early["stop_pct"] == 1.0
