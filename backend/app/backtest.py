"""Backtest der Teststrategien auf historischen 1-Minuten-Kerzen.

Annahmen (bewusst vorsichtig und offen dokumentiert):
* Datenbasis: 1-Minuten-Kerzen (PAPER: Alpaca-Marktdaten, Aktien über IEX; DEMO:
  synthetische Daten). Der Live-Scalper nutzt 10-s-Mikrokerzen – historische
  Kerzen dieser Feinheit liefert Alpaca nicht. Die Durchschnittslängen werden
  deshalb in Minuten angegeben.
* Ein Signal entsteht beim Schluss einer Kerze und wird zum Eröffnungskurs der
  nächsten Kerze ausgeführt.
* Käufe zahlen Kurs + halben Spread, Verkäufe erhalten Kurs − halben Spread.
  Gebühren je Ausführung auf den Orderwert.
* Scalping-Ausstiege innerhalb einer Kerze: Öffnet die Kerze bereits jenseits
  einer Schwelle, gilt der Eröffnungskurs. Werden Stop-Loss und Ziel in derselben
  Kerze berührt, zählt der Stop-Loss (pessimistisch).
* Aktien: nur reguläre Handelszeit (9:30–16:00 New York); Durchschnitte beginnen
  jede Sitzung neu – wie beim Live-Bot.
* Risikogrenzen des aktiven Modus (Einstiege pro Stunde/Tag, Verlustserien-Pause,
  Tagesverlust, max. Orderwert) werden auf Wunsch nachgebildet.
Ein Backtest ist keine Vorhersage. Vergangene Ergebnisse garantieren nichts.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .broker.base import SYMBOL_RE, Bar, is_crypto
from .settings_store import RiskLimits
from .timeutil import NEW_YORK, trading_day

MAX_DISPLAY_BARS = 3000
MAX_EQUITY_POINTS = 500


class BacktestRequest(BaseModel):
    symbol: str
    strategy: Literal["scalp", "sma"] = "scalp"
    days: int = Field(3, ge=1, le=14, description="Zeitraum in Tagen bis jetzt")
    short_window: int = Field(1, ge=1, le=200, description="Kurzer Durchschnitt (Minuten)")
    long_window: int = Field(3, ge=2, le=400, description="Langer Durchschnitt (Minuten)")
    order_value: float = Field(50.0, gt=0, le=100_000)
    stop_loss_pct: float = Field(0.3, gt=0, le=20)
    take_profit_pct: float = Field(0.6, gt=0, le=50)
    max_hold_min: float = Field(10.0, gt=0, le=1440)
    fee_pct: float = Field(0.25, ge=0, le=2)
    spread_pct: float = Field(0.05, ge=0, le=5, description="Angenommener Spread")
    apply_risk_limits: bool = True

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not SYMBOL_RE.match(v):
            raise ValueError("Ungültiges Symbol")
        return v

    @model_validator(mode="after")
    def _check(self) -> BacktestRequest:
        if self.short_window >= self.long_window:
            raise ValueError("Kurzer Durchschnitt muss kleiner als der lange sein")
        if self.strategy == "scalp" and not is_crypto(self.symbol):
            raise ValueError("Scalping-Backtest ist (wie der Live-Bot) für Krypto vorgesehen")
        if self.strategy == "sma" and is_crypto(self.symbol):
            raise ValueError("Die SMA-Minutenstrategie ist für US-Aktien/ETFs vorgesehen")
        return self


@dataclass
class _Position:
    entry_time: datetime
    entry_price: float
    qty: float
    stop: float
    target: float
    deadline: datetime


@dataclass
class _Sim:
    trades: list[dict] = field(default_factory=list)
    realized: float = 0.0
    day_realized: dict[str, float] = field(default_factory=dict)
    loss_streak: int = 0
    pause_until: datetime | None = None
    locked_day: str | None = None
    blocked: Counter = field(default_factory=Counter)
    entries: deque = field(default_factory=deque)


def regular_session(bars: list[Bar]) -> list[Bar]:
    """Aktien: nur Kerzen der regulären Handelszeit (Mo–Fr 9:30–16:00 New York)."""
    out = []
    for b in bars:
        local = b.t.astimezone(NEW_YORK)
        minutes = local.hour * 60 + local.minute
        if local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60:
            out.append(b)
    return out


def _aggregate(bars: list[Bar], factor: int) -> list[Bar]:
    if factor <= 1:
        return bars
    out: list[Bar] = []
    for i in range(0, len(bars), factor):
        chunk = bars[i : i + factor]
        out.append(
            Bar(t=chunk[0].t, o=chunk[0].o, h=max(b.h for b in chunk), l=min(b.l for b in chunk),
                c=chunk[-1].c, v=sum(b.v for b in chunk))
        )
    return out


def simulate(bars: list[Bar], req: BacktestRequest, limits: RiskLimits | None, session_reset: bool) -> dict:
    """Reine Simulation (ohne Netzwerk) – liefert Trades, Equity-Kurve und Kennzahlen."""
    hs = req.spread_pct / 200
    fee = req.fee_pct / 100
    order_value = req.order_value
    sim = _Sim()
    pos: _Position | None = None
    pending_entry = False
    pending_exit: str | None = None
    closes: list[float] = []
    sum_s = sum_l = 0.0
    last_nonzero: float | None = None
    session_day: str | None = None
    equity: list[tuple[datetime, float]] = []
    exposure_bars = 0

    def close(at: datetime, price: float, reason: str) -> None:
        nonlocal pos
        assert pos is not None
        gross = (price - pos.entry_price) * pos.qty
        fees = fee * pos.entry_price * pos.qty + fee * price * pos.qty
        net = gross - fees
        sim.realized += net
        day = trading_day(at)
        sim.day_realized[day] = sim.day_realized.get(day, 0.0) + net
        sim.trades.append(
            {
                "entry_time": pos.entry_time.isoformat(),
                "entry_price": round(pos.entry_price, 6),
                "qty": round(pos.qty, 9),
                "exit_time": at.isoformat(),
                "exit_price": round(price, 6),
                "reason": reason,
                "hold_min": round((at - pos.entry_time).total_seconds() / 60, 1),
                "gross": round(gross, 4),
                "fees": round(fees, 4),
                "net": round(net, 4),
            }
        )
        if limits is not None:
            if net < 0:
                sim.loss_streak += 1
                if sim.loss_streak >= limits.loss_streak_limit:
                    sim.pause_until = at + timedelta(minutes=limits.loss_pause_min)
                    sim.loss_streak = 0
            else:
                sim.loss_streak = 0
        pos = None

    def entry_allowed(at: datetime) -> str | None:
        if limits is None:
            return None
        if order_value > limits.max_order_value + 1e-9:
            return "Orderwert über Grenze"
        while sim.entries and sim.entries[0] <= at - timedelta(hours=1):
            sim.entries.popleft()
        if len(sim.entries) >= limits.max_buys_per_hour:
            return "Einstiege pro Stunde"
        day = trading_day(at)
        if sum(1 for t in sim.trades if trading_day(datetime.fromisoformat(t["entry_time"])) == day) + (
            1 if pos else 0
        ) >= limits.max_buys_per_day:
            return "Einstiege pro Tag"
        if sim.pause_until is not None and at < sim.pause_until:
            return "Pause nach Verlustserie"
        if sim.locked_day == day:
            return "Tagesverlust"
        return None

    for i, b in enumerate(bars):
        day = trading_day(b.t)
        if session_reset and day != session_day:
            closes, sum_s, sum_l, last_nonzero = [], 0.0, 0.0, None
        session_day = day

        # 1) Ausführung vorgemerkter Orders zum Eröffnungskurs
        if pending_exit and pos is not None:
            close(b.t, b.o * (1 - hs), pending_exit)
        pending_exit = None
        if pending_entry and pos is None:
            px = b.o * (1 + hs)
            pos = _Position(
                entry_time=b.t,
                entry_price=px,
                qty=order_value / px,
                stop=px * (1 - req.stop_loss_pct / 100),
                target=px * (1 + req.take_profit_pct / 100),
                deadline=b.t + timedelta(minutes=req.max_hold_min),
            )
            sim.entries.append(b.t)
        pending_entry = False

        # 2) Scalping: Ausstiege innerhalb der Kerze (Geldkurs = Kurs − halber Spread)
        if pos is not None and req.strategy == "scalp":
            bo, bl, bh, bc = (x * (1 - hs) for x in (b.o, b.l, b.h, b.c))
            end = b.t + timedelta(minutes=1)
            if bo <= pos.stop and pos.entry_time != b.t:
                close(end, bo, "Stop-Loss")
            elif bo >= pos.target and pos.entry_time != b.t:
                close(end, bo, "Take-Profit")
            elif bl <= pos.stop:
                close(end, pos.stop, "Stop-Loss")
            elif bh >= pos.target:
                close(end, pos.target, "Take-Profit")
            elif end >= pos.deadline:
                close(end, bc, "Max. Haltedauer")

        # 3) Bewertung (realisiert + offene Position zum Geldkurs, abzüglich Einstiegsgebühr)
        unrealized = 0.0
        if pos is not None:
            exposure_bars += 1
            unrealized = (b.c * (1 - hs) - pos.entry_price) * pos.qty - fee * pos.entry_price * pos.qty
        equity.append((b.t, sim.realized + unrealized))
        if limits is not None and sim.day_realized.get(day, 0.0) + unrealized <= -limits.max_daily_loss:
            sim.locked_day = day

        # 4) Signal beim Kerzenschluss (inkrementelle Durchschnitte)
        closes.append(b.c)
        sum_s += b.c
        sum_l += b.c
        if len(closes) > req.short_window:
            sum_s -= closes[-req.short_window - 1]
        if len(closes) > req.long_window:
            sum_l -= closes[-req.long_window - 1]
        if len(closes) < req.long_window:
            continue
        diff = sum_s / req.short_window - sum_l / req.long_window
        cross_up = diff > 0 and last_nonzero is not None and last_nonzero < 0
        cross_down = diff < 0 and last_nonzero is not None and last_nonzero > 0
        if diff != 0:
            last_nonzero = diff
        if i == len(bars) - 1:
            break  # keine nächste Kerze zur Ausführung
        if cross_up and pos is None:
            why = entry_allowed(bars[i + 1].t)
            if why:
                sim.blocked[why] += 1
            else:
                pending_entry = True
        elif cross_down and pos is not None and req.strategy == "sma":
            pending_exit = "SMA-Kreuzung nach unten"

    if pos is not None and bars:
        last = bars[-1]
        close(last.t + timedelta(minutes=1), last.c * (1 - hs), "Ende des Zeitraums")
        equity[-1] = (equity[-1][0], sim.realized)

    return _report(bars, req, sim, equity, exposure_bars)


def _report(bars: list[Bar], req: BacktestRequest, sim: _Sim, equity: list, exposure_bars: int) -> dict:
    trades = sim.trades
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    peak, max_dd = 0.0, 0.0
    for _, eq in equity:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    step = max(1, math.ceil(len(equity) / MAX_EQUITY_POINTS))
    curve = [{"t": t.isoformat(), "equity": round(eq, 4)} for t, eq in equity[::step]]
    if equity and (not curve or curve[-1]["t"] != equity[-1][0].isoformat()):
        curve.append({"t": equity[-1][0].isoformat(), "equity": round(equity[-1][1], 4)})
    bh_pct = (bars[-1].c / bars[0].o - 1) * 100 if bars else None
    metrics = {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "gross_pnl": round(sum(t["gross"] for t in trades), 2),
        "fees": round(sum(t["fees"] for t in trades), 2),
        "net_pnl": round(sum(nets), 2),
        "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
        "expectancy": round(sum(nets) / len(trades), 4) if trades else None,
        "max_drawdown": round(max_dd, 2),
        "avg_hold_min": round(sum(t["hold_min"] for t in trades) / len(trades), 1) if trades else None,
        "exposure_pct": round(exposure_bars / len(bars) * 100, 1) if bars else None,
        "exit_reasons": dict(Counter(t["reason"] for t in trades)),
        "blocked_entries": dict(sim.blocked),
        "buy_hold_pct": round(bh_pct, 3) if bh_pct is not None else None,
        "net_pct_of_order_value": round(sum(nets) / req.order_value * 100, 2) if trades else 0.0,
    }
    factor = max(1, math.ceil(len(bars) / MAX_DISPLAY_BARS))
    display = _aggregate(bars, factor)
    return {
        "metrics": metrics,
        "trades": trades[-500:],
        "equity": curve,
        "bars": [
            {"t": b.t.isoformat(), "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v} for b in display
        ],
        "display_timeframe_min": factor,
        "bar_count": len(bars),
        "first_bar": bars[0].t.isoformat() if bars else None,
        "last_bar": bars[-1].t.isoformat() if bars else None,
    }


ASSUMPTIONS = [
    "1-Minuten-Kerzen; der Live-Scalper rechnet mit 10-Sekunden-Mikrokerzen.",
    "Signal beim Kerzenschluss, Ausführung zum Eröffnungskurs der nächsten Kerze.",
    "Kauf zum Kurs + halber Spread, Verkauf zum Kurs − halber Spread; Gebühr je Ausführung.",
    "Berühren Stop-Loss und Ziel dieselbe Kerze, zählt der Stop-Loss (pessimistisch).",
    "Aktien: nur reguläre Handelszeit, Durchschnitte je Sitzung neu.",
    "Vergangene Ergebnisse sind keine Vorhersage.",
]


async def run_backtest(ctx, mode: str, req: BacktestRequest, limits: RiskLimits) -> dict:
    rt = ctx.runtimes[mode]
    now = ctx._now()
    end = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    start = end - timedelta(days=req.days)
    raw = await rt.broker.get_bars_range(req.symbol, "1Min", start, end, max_bars=req.days * 1440 + 10)
    crypto = is_crypto(req.symbol)
    bars = raw if crypto else regular_session(raw)
    result = simulate(bars, req, limits if req.apply_risk_limits else None, session_reset=not crypto)
    result.update(
        {
            "mode": mode,
            "request": req.model_dump(),
            "created_at": now.astimezone(timezone.utc).isoformat(),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "source": rt.broker.data_source_label,
            "assumptions": ASSUMPTIONS,
            "risk_limits": limits.model_dump() if req.apply_risk_limits else None,
        }
    )
    if not bars:
        result["warning"] = "Keine Kerzen im Zeitraum (Markt geschlossen oder keine Daten für dieses Symbol)."
    return result
