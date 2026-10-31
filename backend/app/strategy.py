"""Beispielstrategie zum Prüfen der Automatik: Kreuzung zweier gleitender Durchschnitte.

Hinweis: Dies ist KEINE validierte oder profitable Strategie. Sie dient nur
dazu, die Automatisierung (Signale, Orderweg, Risikoprüfung) nachvollziehbar
zu testen. Es wird kein Sprachmodell für Entscheidungen verwendet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .broker.base import Bar

BAR_LENGTH = timedelta(minutes=1)
COMPLETION_GRACE = timedelta(seconds=2)


@dataclass
class Signal:
    kind: str  # buy | sell | none
    bar_time: datetime | None
    close: float | None
    sma_short: float | None
    sma_long: float | None
    reason: str


def complete_bars(bars: list[Bar], now: datetime) -> list[Bar]:
    """Nur vollständig abgeschlossene Minutenkerzen (Kerzenbeginn + 1 Min. liegt in der Vergangenheit)."""
    return [b for b in sorted(bars, key=lambda x: x.t) if b.t + BAR_LENGTH <= now - COMPLETION_GRACE]


def session_bars(bars: list[Bar], session_open: datetime | None) -> list[Bar]:
    """Nur Kerzen ab Beginn der laufenden Handelssitzung (None = kein Filter, z. B. DEMO).

    Verhindert, dass der lange Durchschnitt kurz nach Eröffnung noch Kerzen vom
    Vortag enthält und eine Kurslücke über Nacht als Kreuzen gewertet wird.
    """
    if session_open is None:
        return bars
    return [b for b in bars if b.t >= session_open]


def sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


def evaluate(bars: list[Bar], short: int, long: int) -> Signal:
    """Signal nur bei einem tatsächlichen Kreuzen auf der letzten (abgeschlossenen) Kerze."""
    if short >= long:
        raise ValueError("short muss kleiner als long sein")
    last = bars[-1] if bars else None
    if len(bars) < long + 1:
        return Signal(
            "none",
            last.t if last else None,
            last.c if last else None,
            None,
            None,
            f"Zu wenige Kerzen ({len(bars)}/{long + 1}) für die Berechnung",
        )
    closes = [b.c for b in bars]
    diffs: list[float] = []
    for i in range(long - 1, len(closes)):
        window = closes[: i + 1]
        diffs.append(sma(window, short) - sma(window, long))
    s_short, s_long = sma(closes, short), sma(closes, long)
    cur = diffs[-1]
    prev = next((d for d in reversed(diffs[:-1]) if d != 0), None)
    if cur > 0 and prev is not None and prev < 0:
        return Signal("buy", last.t, last.c, s_short, s_long, f"SMA{short} kreuzt SMA{long} von unten")
    if cur < 0 and prev is not None and prev > 0:
        return Signal("sell", last.t, last.c, s_short, s_long, f"SMA{short} kreuzt SMA{long} von oben")
    side = "über" if cur > 0 else ("unter" if cur < 0 else "gleich")
    return Signal("none", last.t, last.c, s_short, s_long, f"Kein Kreuzen (SMA{short} {side} SMA{long})")


TREND_SLOPE_BARS = 5  # Steigung: Durchschnitt jetzt gegenüber vor 5 Minuten


def _px(value: float) -> str:
    return f"{value:.2f}" if value >= 1 else f"{value:.6f}"


def trend_ok(closes: list[float], minutes: int, price: float) -> tuple[bool, str]:
    """Trendfilter: Einstieg nur, wenn der Kurs über einem steigenden Durchschnitt der Minutenkerzen liegt.

    ``closes``: Schlusskurse abgeschlossener 1-Minuten-Kerzen (älteste zuerst). Bei zu wenigen
    Kerzen gibt es keinen Einstieg – lieber ein Signal auslassen als blind kaufen.
    """
    need = minutes + TREND_SLOPE_BARS
    if len(closes) < need:
        return False, f"zu wenige Minutenkerzen für den Trend ({len(closes)}/{need})"
    now_avg = sum(closes[-minutes:]) / minutes
    before = sum(closes[-minutes - TREND_SLOPE_BARS : -TREND_SLOPE_BARS]) / minutes
    if price <= now_avg:
        return False, f"Kurs {_px(price)} nicht über dem {minutes}-Min.-Durchschnitt {_px(now_avg)}"
    if now_avg < before:
        return False, f"{minutes}-Min.-Durchschnitt fällt ({_px(before)} → {_px(now_avg)})"
    return True, f"Trend aufwärts (Kurs über steigendem {minutes}-Min.-Durchschnitt {_px(now_avg)})"


ATR_STOP_RANGE = (0.05, 10.0)  # Stop-Loss in %: nie enger/weiter als das
ATR_TARGET_RANGE = (0.05, 20.0)
ATR_TARGET_COST_MARGIN = 0.1  # Ziel mindestens Kosten je Trade + 0,1 %


def atr_pct(bars: list[Bar], periods: int, bar_seconds: int) -> float | None:
    """Average True Range der letzten ``periods`` Kerzen in % des letzten Schlusskurses.

    ``bars``: abgeschlossene Kerzen, älteste zuerst. Nach einer Lücke (Börsenschluss,
    Wochenende) zählt nur die Spanne der Kerze, nicht der Sprung zum vorigen Schluss.
    """
    if len(bars) < periods + 1 or bars[-1].c <= 0:
        return None
    ranges = []
    for prev, b in zip(bars, bars[1:]):
        gap = (b.t - prev.t).total_seconds() > 2 * bar_seconds
        tr = b.h - b.l if gap else max(b.h - b.l, abs(b.h - prev.c), abs(b.l - prev.c))
        ranges.append(tr)
    last = ranges[-periods:]
    return sum(last) / periods / bars[-1].c * 100


def atr_exit_pcts(atr: float, stop_mult: float, target_mult: float, fee_pct: float) -> tuple[float, float]:
    """Stop-Loss/Take-Profit in % aus der ATR – begrenzt; das Ziel liegt immer über den Kosten."""
    sl = min(max(atr * stop_mult, ATR_STOP_RANGE[0]), ATR_STOP_RANGE[1])
    tp = min(max(atr * target_mult, ATR_TARGET_RANGE[0], 2 * fee_pct + ATR_TARGET_COST_MARGIN), ATR_TARGET_RANGE[1])
    return round(sl, 4), round(tp, 4)
