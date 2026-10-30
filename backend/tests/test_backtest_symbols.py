"""Backtest, Kerzenintervalle und Symbolvorschläge (Mock-Daten, keine Orders)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.backtest import BacktestRequest, regular_session, simulate
from app.broker.base import Bar
from app.core import AppContext
from app.main import create_app
from app.settings_store import RiskLimits
from app.strategy import evaluate

T = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)  # Samstag: Krypto handelt
H = {"X-Requested-With": "alpaca-bot-ui"}


def series(closes: list[float], hl: float = 0.0, start: datetime = T) -> list[Bar]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Bar(t=start + timedelta(minutes=i), o=prev, h=max(prev, c) + hl, l=min(prev, c) - hl, c=c, v=1))
        prev = c
    return out


def req(**kw) -> BacktestRequest:
    base = dict(symbol="BTC/USD", strategy="scalp", short_window=2, long_window=4, order_value=100,
                stop_loss_pct=1.0, take_profit_pct=2.0, max_hold_min=60, fee_pct=0.25, spread_pct=0.0,
                apply_risk_limits=False)
    return BacktestRequest(**{**base, **kw})


def test_take_profit_trade_with_fees_and_next_open_execution():
    # Bei 99 sind SMA2 und SMA4 gleich (97,5) – das Kreuzen entsteht erst bei 100 (wie live).
    closes = [100, 99, 98, 97, 96, 99, 100, 101, 102, 103]
    res = simulate(series(closes), req(), None, session_reset=False)
    t = res["trades"][0]
    assert t["entry_price"] == 100.0  # Eröffnung der Folgekerze (= Schluss der Signalkerze)
    assert t["reason"] == "Take-Profit" and t["exit_price"] == pytest.approx(102.0)
    gross = (102.0 - 100.0) * 1.0
    fees = 0.0025 * 100 + 0.0025 * 102.0
    assert t["net"] == pytest.approx(gross - fees, abs=1e-4)
    assert res["metrics"]["trades"] == 1 and res["metrics"]["fees"] == pytest.approx(fees, abs=0.01)


def test_stop_loss_wins_if_both_hit_in_same_bar():
    closes = [100, 99, 98, 97, 96, 99, 99]
    bars = series(closes)
    bars.append(Bar(t=bars[-1].t + timedelta(minutes=1), o=99, h=105, l=90, c=99, v=1))  # berührt beides
    res = simulate(bars, req(), None, session_reset=False)
    assert res["trades"][0]["reason"] == "Stop-Loss"
    assert res["trades"][0]["exit_price"] == pytest.approx(99 * 0.99)


def test_spread_makes_fills_worse():
    closes = [100, 99, 98, 97, 96, 99, 100, 101, 102, 103]
    t = simulate(series(closes), req(spread_pct=0.2), None, session_reset=False)["trades"][0]
    assert t["entry_price"] == pytest.approx(100 * 1.001)


def test_time_exit_and_end_of_period():
    flat = [100, 99, 98, 97, 96, 99] + [99.2] * 20
    res = simulate(series(flat), req(max_hold_min=5), None, session_reset=False)
    assert res["trades"][0]["reason"] == "Max. Haltedauer"
    res = simulate(series([100, 99, 98, 97, 96, 99, 100, 100.2]), req(), None, session_reset=False)
    assert res["trades"][-1]["reason"] == "Ende des Zeitraums"


def test_incremental_cross_matches_live_strategy():
    rng = random.Random(7)
    closes = [100.0]
    for _ in range(400):
        closes.append(round(closes[-1] * (1 + rng.uniform(-0.004, 0.004)), 2))
    bars = series(closes)
    expected = [i for i in range(5, len(bars) - 1) if evaluate(bars[: i + 1], 2, 4).kind == "buy"]
    r = req(symbol="SPY", strategy="sma", stop_loss_pct=19, take_profit_pct=50)
    res = simulate(bars, r, None, session_reset=False)
    entries = [datetime.fromisoformat(t["entry_time"]) for t in res["trades"]]
    # Jeder Einstieg liegt eine Kerze nach einem Kaufsignal der Live-Logik
    signal_times = {bars[i + 1].t for i in expected}
    assert entries and all(e in signal_times for e in entries)


def test_risk_limits_block_entries():
    rng = random.Random(3)
    closes = [100.0]
    for _ in range(600):
        closes.append(closes[-1] * (1 + rng.uniform(-0.003, 0.003)))
    limits = RiskLimits(max_buys_per_hour=2, max_buys_per_day=1000, max_order_value=1000)
    res = simulate(series(closes, hl=0.05), req(stop_loss_pct=0.1, take_profit_pct=0.1), limits, session_reset=False)
    by_hour: dict[int, int] = {}
    for t in res["trades"]:
        h = datetime.fromisoformat(t["entry_time"]).hour
        by_hour[h] = by_hour.get(h, 0) + 1
    assert max(by_hour.values()) <= 2
    assert res["metrics"]["blocked_entries"].get("Einstiege pro Stunde", 0) > 0


def test_loss_streak_pause():
    # Fallender Markt mit wiederholten kleinen Erholungen → Verluste in Folge
    closes = []
    for k in range(12):
        base = 100 - k * 2
        closes += [base, base - 0.5, base - 1, base - 1.5, base - 0.6, base - 0.3, base - 3]
    limits = RiskLimits(loss_streak_limit=2, loss_pause_min=600, max_buys_per_hour=100, max_buys_per_day=100,
                        max_order_value=1000, max_daily_loss=10_000)
    res = simulate(series(closes), req(stop_loss_pct=1.0, take_profit_pct=5.0), limits, session_reset=False)
    assert res["metrics"]["losses"] == 2
    assert res["metrics"]["blocked_entries"].get("Pause nach Verlustserie", 0) > 0


def test_regular_session_filter():
    # 13:00–21:00 UTC am Freitag 30.10.2026 → 9:00–17:00 New York (EDT)
    bars = series([100.0] * 480, start=datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc))
    kept = regular_session(bars)
    assert kept[0].t == datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc)
    assert kept[-1].t == datetime(2026, 10, 30, 19, 59, tzinfo=timezone.utc)


def test_request_validation():
    assert req(symbol="SPY").symbol == "SPY"  # Scalping auch für Aktien/ETFs
    with pytest.raises(ValueError):
        req(symbol="BTC/USD", strategy="sma")  # SMA-Minutenstrategie nur Aktien/ETFs
    with pytest.raises(ValueError):
        req(short_window=4, long_window=4)
    with pytest.raises(ValueError):
        req(days=30)


# ----------------------------------------------------------------- API
@pytest.fixture
def client(settings, paper_broker, clock):
    app = create_app(settings, context_factory=lambda s, db: AppContext(s, db, paper_broker=paper_broker, now=clock),
                     background=False)
    with TestClient(app) as c:
        yield c


def test_backtest_api_demo_runs_and_is_stored(client):
    body = {"expected_mode": "DEMO", "symbol": "BTC/USD", "strategy": "scalp", "days": 1, "short_window": 1,
            "long_window": 3, "order_value": 50, "stop_loss_pct": 0.3, "take_profit_pct": 0.6, "max_hold_min": 10,
            "fee_pct": 0.25, "spread_pct": 0.05, "apply_risk_limits": True}
    r = client.post("/api/backtests", json=body, headers=H)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["mode"] == "DEMO" and "DEMO" in res["source"]
    assert res["bar_count"] >= 1400 and len(res["bars"]) <= 3000
    assert res["metrics"]["trades"] > 0 and res["equity"]
    runs = client.get("/api/backtests").json()["runs"]
    assert runs[0]["id"] == res["id"] and runs[0]["metrics"]["trades"] == res["metrics"]["trades"]
    full = client.get(f"/api/backtests/{res['id']}").json()
    assert full["metrics"] == res["metrics"]
    assert client.post("/api/backtests", json={**body, "expected_mode": "PAPER"}, headers=H).status_code == 409


def test_backtest_api_paper_uses_paginated_real_data(client, fake, clock):
    start = clock().replace(second=0, microsecond=0) - timedelta(days=1)
    rng = random.Random(1)
    closes = [100.0]
    for _ in range(1500):
        closes.append(closes[-1] * (1 + rng.uniform(-0.003, 0.003)))
    fake.bars_series("BTC/USD", start, closes, hl=0.05)
    client.post("/api/mode", json={"mode": "PAPER"}, headers=H)
    body = {"expected_mode": "PAPER", "symbol": "BTC/USD", "strategy": "scalp", "days": 1, "short_window": 2,
            "long_window": 6, "apply_risk_limits": False}
    res = client.post("/api/backtests", json=body, headers=H).json()
    assert res["bar_count"] > 1000 and "Alpaca" in res["source"]
    assert fake.count("POST", "/v2/orders") == 0  # Backtest sendet nie Orders


def test_bars_timeframes(client):
    for tf in ("1Min", "5Min", "15Min", "1Hour", "1Day"):
        r = client.get(f"/api/bars?symbol=BTC/USD&timeframe={tf}&limit=50").json()
        assert r["timeframe"] == tf and len(r["bars"]) == 50 and r["status"] == "ok", (tf, r["status"], r["message"])
        times = [b["t"] for b in r["bars"]]
        assert times == sorted(times)
    assert client.get("/api/bars?symbol=BTC/USD&timeframe=2Min").status_code == 422


def test_crypto_market_always_open_in_bars(client, fake):
    client.post("/api/mode", json={"mode": "PAPER"}, headers=H)
    fake.is_open = False
    client.post("/api/sync", headers=H)
    fake.bars_series("BTC/USD", datetime.now(timezone.utc) - timedelta(minutes=30), [100.0] * 25)
    r = client.get("/api/bars?symbol=BTC/USD").json()
    assert r["market_open"] is True and r["status"] != "market_closed"


def test_symbol_suggestions_demo_and_search(client, fake):
    demo = client.get("/api/symbols").json()
    symbols = [i["symbol"] for i in demo["items"]]
    assert symbols[0] == "BTC/USD" and demo["items"][0]["group"] == "Bot"
    assert "SPY" in symbols and "TSLA" not in symbols  # DEMO kennt TSLA nicht
    assert [i["symbol"] for i in client.get("/api/symbols?q=eth").json()["items"]] == ["ETH/USD"]
    client.post("/api/mode", json={"mode": "PAPER"}, headers=H)
    fake.assets["ABNB"] = {"symbol": "ABNB", "name": "Airbnb", "class": "us_equity", "exchange": "NASDAQ",
                           "status": "active", "tradable": True}
    res = client.get("/api/symbols?q=AB").json()
    assert any(i["symbol"] == "ABNB" and i["group"] == "Suche" for i in res["items"])
