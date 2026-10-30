"""Strategiesignale, einmalige Kerzenverarbeitung, Mehrfachstarts und Startbedingungen."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.bot import BotStartError
from app.broker.base import Bar
from app.core import AppContext
from app.settings_store import BotParams, set_bot_params
from app.strategy import complete_bars, evaluate, session_bars

T = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)


def mk(closes: list[float]) -> list[Bar]:
    return [Bar(t=T + timedelta(minutes=i), o=c, h=c, l=c, c=c) for i, c in enumerate(closes)]


DOWN_THEN_UP = [10.0] * 3 + [9.0] * 3 + [12.0]  # SMA2/SMA4 kreuzen auf der letzten Kerze nach oben


def test_signal_only_on_actual_cross():
    sig = evaluate(mk(DOWN_THEN_UP), 2, 4)
    assert sig.kind == "buy"
    # Eine Kerze später ist SMA2 weiterhin über SMA4 → kein neues Signal
    assert evaluate(mk(DOWN_THEN_UP + [12.5]), 2, 4).kind == "none"


def test_sell_cross_and_no_signal_in_trend():
    up_then_down = [10.0] * 3 + [11.0] * 3 + [8.0]
    assert evaluate(mk(up_then_down), 2, 4).kind == "sell"
    assert evaluate(mk([float(i) for i in range(1, 12)]), 2, 4).kind == "none"


def test_cross_through_equality_counts_once():
    # Differenz: negativ → 0 → positiv
    closes = [10, 10, 10, 9, 9, 10, 10, 11]
    kinds = [evaluate(mk([float(c) for c in closes[: i + 1]]), 2, 4).kind for i in range(4, len(closes))]
    assert kinds.count("buy") == 1


def test_not_enough_bars():
    sig = evaluate(mk([1.0, 2.0, 3.0]), 2, 4)
    assert sig.kind == "none" and "Zu wenige" in sig.reason


def test_incomplete_bar_excluded():
    bars = mk([1.0, 2.0, 3.0])
    now = bars[-1].t + timedelta(seconds=30)  # letzte Minute läuft noch
    assert [b.t for b in complete_bars(bars, now)] == [b.t for b in bars[:-1]]


def test_invalid_windows_rejected():
    with pytest.raises(ValueError):
        BotParams(short_window=30, long_window=10)


# ------------------------------------------------------------------ Bot
def setup_signal(fake, closes: list[float]) -> None:
    fake.make_bars("SPY", closes)


async def start_paper_bot(ctx: AppContext, fake) -> None:
    set_bot_params(ctx.db, "PAPER", BotParams(strategy="sma", symbol="SPY", short_window=2, long_window=4, order_value=5))
    setup_signal(fake, [10.0] * 10)
    await ctx.bot.start("PAPER")


async def test_double_start_rejected(paper_ctx: AppContext, fake):
    await start_paper_bot(paper_ctx, fake)
    with pytest.raises(BotStartError, match="läuft bereits"):
        await paper_ctx.bot.start("PAPER")
    await paper_ctx.bot.pause()


async def test_parallel_starts_only_one_instance(paper_ctx: AppContext, fake):
    set_bot_params(paper_ctx.db, "PAPER", BotParams(strategy="sma", symbol="SPY", short_window=2, long_window=4, order_value=5))
    setup_signal(fake, [10.0] * 10)
    results = await asyncio.gather(*[paper_ctx.bot.start("PAPER") for _ in range(5)], return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, BotStartError) for r in results) == 4
    await paper_ctx.bot.pause()


@pytest.mark.parametrize(
    "prep,msg",
    [
        (lambda c, f: c.set_orders_enabled("PAPER", False), "Orderfreigabe"),
        (lambda c, f: setattr(f, "is_open", False), "geschlossen"),
        (lambda c, f: f.account.update(status="ACCOUNT_UPDATED", trading_blocked=True), "Handelsstatus"),
        (lambda c, f: f.make_bars("SPY", [10.0] * 10, end_minute=f.clock() - timedelta(minutes=30)), "nicht frisch"),
    ],
)
async def test_start_preconditions(paper_ctx: AppContext, fake, prep, msg):
    set_bot_params(paper_ctx.db, "PAPER", BotParams(strategy="sma", symbol="SPY", short_window=2, long_window=4, order_value=5))
    setup_signal(fake, [10.0] * 10)
    prep(paper_ctx, fake)
    with pytest.raises(BotStartError, match=msg):
        await paper_ctx.bot.start("PAPER")
    assert paper_ctx.bot.running is False


async def test_start_fails_on_connection_error(paper_ctx: AppContext, fake):
    import httpx

    fake.inject[("GET", "/v2/account")] = [httpx.ConnectError("x")] * 3
    with pytest.raises(BotStartError, match="Kontoabgleich"):
        await paper_ctx.bot.start("PAPER")


async def test_each_bar_processed_once_and_signal_not_repeated(paper_ctx: AppContext, fake, clock):
    await start_paper_bot(paper_ctx, fake)
    bot = paper_ctx.bot
    # Neue Kerze mit Aufwärtskreuzung
    clock.advance(minutes=1)
    setup_signal(fake, [10.0] * 3 + [9.0] * 3 + [12.0])
    paper_ctx.market.clear()
    await bot.tick("PAPER")
    assert fake.count("POST", "/v2/orders") == 1
    # Gleiche Kerze erneut → nichts
    paper_ctx.market.clear()
    await bot.tick("PAPER")
    await bot.tick("PAPER")
    assert fake.count("POST", "/v2/orders") == 1
    decisions = paper_ctx.db.list_decisions("PAPER")
    assert len(decisions) == 1 and decisions[0]["action"] == "order"
    # Nächste Kerze: erneutes Kaufsignal? Nein – SMA bleibt oben, kein Kreuzen
    clock.advance(minutes=1)
    setup_signal(fake, [10.0] * 3 + [9.0] * 3 + [12.0, 12.5])
    paper_ctx.market.clear()
    await bot.tick("PAPER")
    assert fake.count("POST", "/v2/orders") == 1
    await bot.pause()


async def test_bot_buys_only_without_position_or_open_buy(paper_ctx: AppContext, fake, clock):
    await start_paper_bot(paper_ctx, fake)
    fake.add_order(symbol="SPY", side="buy", notional="5", status="new")
    clock.advance(minutes=1)
    setup_signal(fake, [10.0] * 3 + [9.0] * 3 + [12.0])
    paper_ctx.market.clear()
    await paper_ctx.bot.tick("PAPER")
    assert fake.count("POST", "/v2/orders") == 0
    assert "offene Kauforder" in paper_ctx.db.list_decisions("PAPER")[0]["reason"]
    await paper_ctx.bot.pause()


async def test_bot_sell_only_available_and_skip_without_position(paper_ctx: AppContext, fake, clock):
    await start_paper_bot(paper_ctx, fake)
    clock.advance(minutes=1)
    setup_signal(fake, [10.0] * 3 + [11.0] * 3 + [8.0])
    paper_ctx.market.clear()
    await paper_ctx.bot.tick("PAPER")
    assert fake.count("POST", "/v2/orders") == 0
    assert "kein verfügbarer Bestand" in paper_ctx.db.list_decisions("PAPER")[0]["reason"]
    # Mit Position: verkauft genau den verfügbaren Bestand
    fake.add_position("SPY", 3, 8.0)
    fake.add_order(symbol="SPY", side="sell", qty="1", status="new")
    clock.advance(minutes=1)
    setup_signal(fake, [10.0] * 4 + [11.0] * 3 + [8.0])
    paper_ctx.market.clear()
    await paper_ctx.bot.tick("PAPER")
    assert fake.count("POST", "/v2/orders") == 1
    posted = [o for o in fake.orders.values() if o["client_order_id"].startswith("apb-pb-")]
    assert posted[0]["side"] == "sell" and posted[0]["qty"] == "2"
    await paper_ctx.bot.pause()


async def test_pause_keeps_positions_and_orders(paper_ctx: AppContext, fake):
    fake.add_position("SPY", 3, 10.0)
    open_order = fake.add_order(symbol="SPY", side="sell", qty="1", status="new")
    await start_paper_bot(paper_ctx, fake)
    await paper_ctx.bot.pause()
    assert paper_ctx.bot.running is False
    assert fake.orders[open_order["id"]]["status"] == "new"
    assert fake.count("DELETE", f"/v2/orders/{open_order['id']}") == 0
    assert len(fake.positions) == 1


async def test_mode_switch_pauses_bot(ctx: AppContext):
    set_bot_params(ctx.db, "DEMO", BotParams(strategy="sma", symbol="SPY", short_window=2, long_window=4))
    await ctx.bot.start("DEMO")
    assert ctx.bot.running
    await ctx.set_mode("PAPER")
    assert ctx.bot.running is False


async def test_demo_bot_runs_without_alpaca(ctx: AppContext, fake, clock):
    set_bot_params(ctx.db, "DEMO", BotParams(strategy="sma", symbol="SPY", short_window=2, long_window=4, order_value=20))
    await ctx.bot.start("DEMO")
    for _ in range(60):
        clock.advance(minutes=1)
        ctx.market.clear()
        await ctx.bot.tick("DEMO")
    decisions = ctx.db.list_decisions("DEMO", 500)
    assert len(decisions) == 60  # jede Kerze genau einmal
    assert any(d["action"] == "order" for d in decisions)
    assert fake.calls == []  # DEMO berührt Alpaca nie
    await ctx.bot.pause()


# ------------------------------------------------------- Sitzungsfilter
def test_session_bars_drop_previous_day():
    session_open = T + timedelta(minutes=3)
    bars = mk([1.0, 2.0, 3.0, 4.0, 5.0])
    assert [b.c for b in session_bars(bars, session_open)] == [4.0, 5.0]
    assert session_bars(bars, None) == bars  # DEMO: kein Filter


async def test_calendar_session_open_converted_to_utc(paper_broker, fake, clock):
    opened = await paper_broker.get_session_open(clock())
    assert opened == datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc)  # 09:30 EDT
    await paper_broker.get_session_open(clock())
    assert fake.count("GET", "/v2/calendar") == 1  # pro Tag zwischengespeichert
    # Nach Ende der Sommerzeit in New York (1.11.2026): 09:30 EST = 14:30 UTC
    later = await paper_broker.get_session_open(datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc))
    assert later == datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)


async def test_overnight_gap_does_not_trigger_signal(paper_ctx: AppContext, fake, clock):
    """Kurz nach Eröffnung: Vortageskerzen bleiben draussen, eine Kurslücke löst kein Signal aus."""
    from datetime import datetime as dt

    set_bot_params(paper_ctx.db, "PAPER", BotParams(strategy="sma", symbol="SPY", short_window=2, long_window=4, order_value=5))
    # 13:33 UTC = 09:33 New York → erst 3 Kerzen der laufenden Sitzung
    clock.t = dt(2026, 10, 30, 13, 33, 5, tzinfo=timezone.utc)
    # Vor Eröffnung leichter Abwärtstrend, heute Kurslücke nach oben: ohne Filter wäre das ein Kaufsignal
    before_open = [11.0, 10.8, 10.6, 10.4, 10.2]
    fake.make_bars("SPY", before_open + [12.0] * 3)
    paper_ctx.market.clear()
    await paper_ctx.bot.start("PAPER")
    clock.advance(minutes=1)
    fake.make_bars("SPY", before_open + [12.0] * 4)
    paper_ctx.market.clear()
    await paper_ctx.bot.tick("PAPER")
    d = paper_ctx.db.list_decisions("PAPER")[0]
    assert d["signal"] == "none" and "laufenden Sitzung" in d["reason"]
    assert fake.count("POST", "/v2/orders") == 0
    # Gegenprobe ohne Filter: dieselben Kerzen hätten ein Kaufsignal ergeben
    from app.broker.base import Bar

    unfiltered = [Bar(t=T + timedelta(minutes=i), o=c, h=c, l=c, c=c) for i, c in enumerate(before_open + [12.0])]
    assert evaluate(unfiltered, 2, 4).kind == "buy"
    await paper_ctx.bot.pause()


async def test_bot_start_refused_on_holiday(paper_ctx: AppContext, fake):
    set_bot_params(paper_ctx.db, "PAPER", BotParams(strategy="sma", symbol="SPY", short_window=2, long_window=4, order_value=5))
    setup_signal(fake, [10.0] * 10)
    fake.holidays.add("2026-10-30")
    with pytest.raises(BotStartError, match="keine reguläre Sitzung"):
        await paper_ctx.bot.start("PAPER")


@pytest.mark.parametrize("raw,ok", [("09:30", True), ("0930", True), ("09:30:00", True), ("bald", False)])
async def test_calendar_open_formats(paper_broker, fake, clock, raw, ok):
    from app.broker.base import BrokerError

    fake.session_open_time = raw
    if ok:
        assert await paper_broker.get_session_open(clock()) == datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc)
    else:
        with pytest.raises(BrokerError):
            await paper_broker.get_session_open(clock())
