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
