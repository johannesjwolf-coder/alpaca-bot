"""Risiko- und Bestandsgrenzen, Orderfreigabe, Duplikate nach Timeouts, Neustartabgleich."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from app.broker.alpaca import AlpacaPaperBroker, EnvAuth
from app.broker.base import LOCAL_NOT_RECEIVED, LOCAL_UNKNOWN
from app.core import AppContext
from app.orders import OrderBlocked, OrderFailed, OrderInput
from app.settings_store import RiskLimits, set_risk_limits


def failed(preview: dict) -> dict[str, str]:
    return {c["name"]: c["detail"] for c in preview["checks"] if c["blocking"] and not c["ok"]}


def buy(notional: str = "5", symbol: str = "SPY") -> OrderInput:
    return OrderInput(symbol=symbol, side="buy", notional=Decimal(notional))


# ------------------------------------------------------------ Freigabe
async def test_paper_orders_blocked_without_release(ctx: AppContext, fake):
    await ctx.set_mode("PAPER")
    assert ctx.orders_enabled("PAPER") is False
    p = await ctx.orders.preview("PAPER", buy())
    assert "Paper-Orderfreigabe" in failed(p)
    with pytest.raises(OrderBlocked):
        await ctx.orders.submit("PAPER", buy(), source="manual", idempotency_key="k1")
    assert fake.count("POST", "/v2/orders") == 0


async def test_release_off_after_restart(settings, db, paper_broker, clock):
    db.set("PAPER", "orders_enabled", True)
    db.set("PAPER", "bot_running", True)
    c = AppContext(settings, db, paper_broker=paper_broker, now=clock)
    await c.startup(background=False)
    assert c.orders_enabled("PAPER") is False
    assert c.mode == "DEMO"
    assert c.bot.running is False
    await c.shutdown()


async def test_mode_switch_disables_release(paper_ctx: AppContext):
    assert paper_ctx.orders_enabled("PAPER")
    await paper_ctx.set_mode("DEMO")
    await paper_ctx.set_mode("PAPER")
    assert paper_ctx.orders_enabled("PAPER") is False


# -------------------------------------------------------------- Risiko
async def test_buy_within_limits_submits(paper_ctx: AppContext, fake):
    res = await paper_ctx.orders.submit("PAPER", buy("5"), source="manual", idempotency_key="ok1")
    assert res["order"]["status"] == "accepted"  # angenommen ≠ ausgeführt
    assert res["order"]["client_order_id"].startswith("apb-pm-")
    assert fake.count("POST", "/v2/orders") == 1


@pytest.mark.parametrize(
    "limits,inp,expected",
    [
        (dict(max_order_value=4), buy("5"), "Orderwert"),
        (dict(max_total_invested=20), buy("5"), "Investierter Gesamtbetrag"),
        (dict(max_open_positions=1), buy("5"), "Offene Positionen"),
    ],
)
async def test_buy_limits(paper_ctx: AppContext, fake, limits, inp, expected):
    fake.add_position("QQQ", 1, 18.0)
    set_risk_limits(paper_ctx.db, "PAPER", RiskLimits(**limits))
    p = await paper_ctx.orders.preview("PAPER", inp)
    assert expected in failed(p)


async def test_orders_per_day_limit_blocks_buys_not_sells(paper_ctx: AppContext, fake):
    set_risk_limits(paper_ctx.db, "PAPER", RiskLimits(max_orders_per_day=1))
    fake.add_position("SPY", 3, 10.0)
    await paper_ctx.orders.submit("PAPER", buy("5"), source="manual", idempotency_key="d1")
    p = await paper_ctx.orders.preview("PAPER", buy("5"))
    assert "Orders pro Tag" in failed(p)
    sell = await paper_ctx.orders.preview("PAPER", OrderInput(symbol="SPY", side="sell", qty=Decimal("1")))
    assert sell["allowed"], failed(sell)


async def test_buying_power_is_not_own_money(paper_ctx: AppContext, fake):
    # Buying Power 4000, aber nur 30 eigenes Cash.
    fake.account.update(cash="30", non_marginable_buying_power="30", buying_power="4000")
    set_risk_limits(paper_ctx.db, "PAPER", RiskLimits(max_order_value=1000, max_total_invested=1000))
    p = await paper_ctx.orders.preview("PAPER", buy("40"))
    assert "Eigene Mittel (ohne Kredit)" in failed(p)
    ok = await paper_ctx.orders.preview("PAPER", buy("25"))
    assert ok["allowed"], failed(ok)


async def test_open_buy_orders_reserve_cash(paper_ctx: AppContext, fake):
    fake.account.update(cash="30", non_marginable_buying_power="30")
    fake.add_order(symbol="SPY", side="buy", notional="20", status="new")
    p = await paper_ctx.orders.preview("PAPER", buy("15"))
    assert "Eigene Mittel (ohne Kredit)" in failed(p)


async def test_sell_only_available_holdings(paper_ctx: AppContext, fake):
    p = await paper_ctx.orders.preview("PAPER", OrderInput(symbol="SPY", side="sell", qty=Decimal("1")))
    assert "Bestand" in failed(p)  # keine Leerverkäufe
    fake.add_position("SPY", 3, 10.0)
    fake.add_order(symbol="SPY", side="sell", qty="2", status="new")  # bindet 2 Stück
    p = await paper_ctx.orders.preview("PAPER", OrderInput(symbol="SPY", side="sell", qty=Decimal("2")))
    assert "Verfügbarer Bestand" in failed(p)
    p = await paper_ctx.orders.preview("PAPER", OrderInput(symbol="SPY", side="sell", qty=Decimal("1")))
    assert p["allowed"], failed(p)


async def test_instrument_and_quantity_rules(paper_ctx: AppContext):
    cases = [
        (OrderInput(symbol="BRK.A", side="buy", qty=Decimal("0.5")), "Menge/Gültigkeit"),
        (OrderInput(symbol="SPY", side="buy", qty=Decimal("0.5"), time_in_force="gtc"), "Menge/Gültigkeit"),
        (OrderInput(symbol="SPY", side="buy", notional=Decimal("0.5")), "Betrag/Gültigkeit"),
        (OrderInput(symbol="SPY", side="buy", notional=Decimal("5.123")), "Betrag/Gültigkeit"),
        (OrderInput(symbol="SPY", side="sell", notional=Decimal("5")), "Betrag/Gültigkeit"),
        (OrderInput(symbol="SPY", side="buy", qty=Decimal("1"), notional=Decimal("5")), "Menge/Betrag"),
        (OrderInput(symbol="OTCX", side="buy", qty=Decimal("1")), "Instrument"),
        (OrderInput(symbol="DEAD", side="buy", qty=Decimal("1")), "Instrument"),
        (OrderInput(symbol="NOPE", side="buy", qty=Decimal("1")), "Instrument"),
    ]
    for inp, name in cases:
        p = await paper_ctx.orders.preview("PAPER", inp)
        assert name in failed(p), (inp, failed(p))


async def test_blocked_account_status(paper_ctx: AppContext, fake):
    fake.account["trading_blocked"] = True
    p = await paper_ctx.orders.preview("PAPER", buy())
    assert "Kontostatus" in failed(p)


# ---------------------------------------------------------- Tagesverlust
async def test_daily_loss_locks_buys_allows_sells_and_resets_next_day(paper_ctx: AppContext, fake, clock):
    fake.add_position("SPY", 3, 10.0)
    fake.account.update(equity="970", last_equity="1000")  # −30 inkl. unrealisiert
    p = await paper_ctx.orders.preview("PAPER", buy())
    assert "Tagesverlust" in failed(p)
    # Erholung am selben Tag hebt die Sperre nicht auf
    fake.account.update(equity="1001")
    p = await paper_ctx.orders.preview("PAPER", buy())
    assert "Tagesverlust" in failed(p)
    sell = await paper_ctx.orders.preview("PAPER", OrderInput(symbol="SPY", side="sell", qty=Decimal("1")))
    assert sell["allowed"], failed(sell)
    # Tageswechsel (New York) → neue Ausgangsbasis, Sperre aufgehoben
    clock.advance(days=1)
    fake.account.update(last_equity="1001")
    p = await paper_ctx.orders.preview("PAPER", buy())
    assert "Tagesverlust" not in failed(p)


async def test_daily_loss_without_data_blocks(paper_ctx: AppContext):
    status = paper_ctx.risk.daily_status("PAPER", None, RiskLimits())
    assert status["data_ok"] is False


async def test_daily_baseline_fallback_first_observation(paper_ctx: AppContext, fake):
    fake.account.update(last_equity="0", equity="900")
    d = paper_ctx.risk.daily_status("PAPER", (await paper_ctx.runtimes["PAPER"].broker.get_account()), RiskLimits())
    assert d["baseline"] == 900 and "Erster beobachteter" in d["baseline_source"]


# ------------------------------------------------------ Timeout/Duplikate
async def test_timeout_after_accept_reconciles_without_resend(paper_ctx: AppContext, fake):
    def accept_then_timeout(request: httpx.Request):
        fake.inject.pop(("POST", "/v2/orders"), None)
        fake.handler(request)  # Broker legt die Order an …
        fake.calls.pop()  # (interner Aufruf, nicht doppelt zählen)
        raise httpx.ReadTimeout("Antwort verloren")  # … aber die Antwort geht verloren

    fake.inject[("POST", "/v2/orders")] = [accept_then_timeout]
    res = await paper_ctx.orders.submit("PAPER", buy(), source="manual", idempotency_key="t1")
    assert fake.count("POST", "/v2/orders") == 1
    assert fake.count("GET", "/v2/orders:by_client_order_id") >= 1
    assert res["order"]["status"] == "accepted"
    assert res["order"]["broker_order_id"] in fake.orders
    # Wiederholtes Absenden desselben Formulars → kein zweiter POST
    again = await paper_ctx.orders.submit("PAPER", buy(), source="manual", idempotency_key="t1")
    assert again["duplicate"] is True
    assert fake.count("POST", "/v2/orders") == 1


async def test_timeout_unknown_blocks_buys_until_resolved(paper_ctx: AppContext, fake, clock):
    fake.inject[("POST", "/v2/orders")] = [httpx.ReadTimeout("weg")]
    res = await paper_ctx.orders.submit("PAPER", buy(), source="manual", idempotency_key="t2")
    assert res["order"]["status"] == LOCAL_UNKNOWN
    p = await paper_ctx.orders.preview("PAPER", buy())
    assert "Zustand abgeglichen" in failed(p)
    assert fake.count("POST", "/v2/orders") == 1  # niemals blind wiederholt
    # Nach Ablauf der Wartezeit und erneuten Abgleichen: nicht angekommen
    clock.advance(seconds=130)
    await paper_ctx.runtimes["PAPER"].sync()
    row = paper_ctx.db.query_one("SELECT status FROM orders WHERE idempotency_key='t2'")
    assert row["status"] == LOCAL_NOT_RECEIVED
    p = await paper_ctx.orders.preview("PAPER", buy())
    assert p["allowed"], failed(p)
    assert fake.count("POST", "/v2/orders") == 1


async def test_broker_rejection_marks_failed(paper_ctx: AppContext, fake):
    fake.inject[("POST", "/v2/orders")] = [httpx.Response(403, json={"message": "insufficient buying power"})]
    with pytest.raises(OrderFailed):
        await paper_ctx.orders.submit("PAPER", buy(), source="manual", idempotency_key="r1")
    row = paper_ctx.db.query_one("SELECT status, message FROM orders WHERE idempotency_key='r1'")
    assert row["status"] == "local_failed" and "insufficient" in row["message"]


async def test_parallel_submits_same_key_send_once(paper_ctx: AppContext, fake):
    results = await asyncio.gather(
        *[paper_ctx.orders.submit("PAPER", buy(), source="manual", idempotency_key="par") for _ in range(5)]
    )
    assert fake.count("POST", "/v2/orders") == 1
    assert sum(1 for r in results if r["duplicate"]) == 4


async def test_parallel_submits_respect_limits(paper_ctx: AppContext, fake):
    set_risk_limits(paper_ctx.db, "PAPER", RiskLimits(max_orders_per_day=2))
    outcomes = await asyncio.gather(
        *[paper_ctx.orders.submit("PAPER", buy(), source="manual", idempotency_key=f"p{i}") for i in range(4)],
        return_exceptions=True,
    )
    assert fake.count("POST", "/v2/orders") == 2
    assert sum(isinstance(o, OrderBlocked) for o in outcomes) == 2


# --------------------------------------------------------- Neustartabgleich
async def test_restart_reconciles_pending_and_partial_fills(settings, db, paper_broker, fake, clock):
    c1 = AppContext(settings, db, paper_broker=paper_broker, now=clock)
    await c1.startup(background=False)
    await c1.set_mode("PAPER")
    c1.set_orders_enabled("PAPER", True)
    res = await c1.orders.submit("PAPER", OrderInput(symbol="SPY", side="buy", qty=Decimal("4")), source="manual", idempotency_key="rs")
    cid = res["order"]["client_order_id"]
    # Absturz direkt nach dem Speichern einer weiteren Order (vor Senden):
    db.execute(
        "INSERT INTO orders (mode, client_order_id, source, symbol, side, qty, time_in_force, status, trading_day, created_at, updated_at) "
        "VALUES ('PAPER', 'apb-pm-crash', 'manual', 'SPY', 'buy', '1', 'day', 'local_pending', '2026-10-30', ?, ?)",
        (clock().isoformat(), clock().isoformat()),
    )
    await c1.shutdown()

    # Während der Ausfallzeit: Teilausführung beim Broker
    order = next(o for o in fake.orders.values() if o["client_order_id"] == cid)
    order.update(status="partially_filled", filled_qty="2", filled_avg_price="10.01")
    fake.add_position("SPY", 2, 10.01)

    # Neuer Prozess → neuer HTTP-Client (gleicher simulierter Broker)
    broker2 = AlpacaPaperBroker(EnvAuth(), transport=httpx.MockTransport(fake.handler), backoff_base_s=0.001)
    c2 = AppContext(settings, db, paper_broker=broker2, now=clock)
    await c2.startup(background=False)
    await c2.set_mode("PAPER")
    rt = c2.runtimes["PAPER"]
    assert rt.reconciled is False
    assert await rt.sync()
    row = db.query_one("SELECT * FROM orders WHERE client_order_id=?", (cid,))
    assert row["status"] == "partially_filled" and row["filled_qty"] == 2.0
    crash = db.query_one("SELECT * FROM orders WHERE client_order_id='apb-pm-crash'")
    assert crash["status"] == "local_pending"  # weiterhin unklar → Käufe gesperrt
    assert rt.state_clear is False
    assert [p.symbol for p in rt.snapshot.positions] == ["SPY"]
    await c2.shutdown()


async def test_connection_loss_requires_reconcile_before_buys(paper_ctx: AppContext, fake):
    rt = paper_ctx.runtimes["PAPER"]
    assert await rt.sync()
    fake.inject[("GET", "/v2/account")] = [httpx.ConnectError("down")] * 3
    assert await rt.sync() is False
    assert rt.connection == "disconnected" and rt.reconciled is False
    p = await paper_ctx.orders.preview("PAPER", buy())  # Abgleich klappt wieder
    assert p["allowed"], failed(p)
    assert rt.reconciled is True
