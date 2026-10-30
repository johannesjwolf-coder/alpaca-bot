"""Marktdaten mit Cache und Frische-Bewertung.

* PAPER: Alpaca Market Data, ausdrücklich ``feed=iex`` (kostenloser Zugang).
  IEX ist nur ein Handelsplatz mit einem kleinen Teil des Gesamtvolumens: Kurse
  können vom konsolidierten Markt abweichen und Minuten ohne IEX-Trade haben
  keine Kerze. Das wird angezeigt und nicht durch erfundene Werte aufgefüllt.
* DEMO: synthetische Daten, deutlich gekennzeichnet.
* Ein Fehler liefert einen Fehlerstatus – niemals Ersatzdaten.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from .broker.base import SYMBOL_RE, TIMEFRAMES, Bar, BrokerError, is_crypto
from .db import iso
from .runtime import ModeRuntime
from .timeutil import utcnow

CACHE_TTL_S = 20.0


class MarketDataService:
    def __init__(self, ttl_s: float = CACHE_TTL_S, now: Callable[[], datetime] = utcnow) -> None:
        self.ttl_s = ttl_s
        self._now = now
        self._cache: dict[tuple[str, str, int, str], tuple[datetime, list]] = {}

    def clear(self) -> None:
        self._cache.clear()

    async def fetch(
        self, rt: ModeRuntime, symbol: str, limit: int, timeframe: str = "1Min"
    ) -> tuple[list[Bar], datetime, bool]:
        """Kerzen mit Cache (schont API-Limits). Wirft BrokerError."""
        now = self._now()
        key = (rt.mode, symbol, limit, timeframe)
        cached = self._cache.get(key)
        if cached and (now - cached[0]).total_seconds() < self.ttl_s:
            return cached[1], cached[0], True
        bars = await rt.broker.get_bars(symbol, limit, timeframe)
        self._cache[key] = (now, bars)
        return bars, now, False

    async def bars(
        self, rt: ModeRuntime, symbol: str, limit: int = 240, max_age_s: int = 180, timeframe: str = "1Min"
    ) -> dict:
        symbol = symbol.strip().upper()
        now = self._now()
        result: dict = {
            "mode": rt.mode,
            "symbol": symbol,
            "source": rt.broker.data_source_label,
            "feed": rt.broker.data_feed,
            "timeframe": timeframe,
            "asset_class": "crypto" if is_crypto(symbol.strip().upper()) else "us_equity",
            "fetched_at": None,
            "cached": False,
            "bars": [],
            "last_bar_at": None,
            "last_complete_at": None,
            "status": "error",
            "message": "",
            "error": None,
            # Krypto wird rund um die Uhr gehandelt; die Börsenuhr gilt nur für Aktien.
            "market_open": True if is_crypto(symbol.strip().upper()) else (rt.snapshot.clock.is_open if rt.snapshot else None),
        }
        if not SYMBOL_RE.match(symbol):
            result["message"] = "Ungültiges Symbol"
            return result
        if timeframe not in TIMEFRAMES:
            result["message"] = "Unbekanntes Intervall"
            return result
        bar_len = timedelta(seconds=TIMEFRAMES[timeframe])
        try:
            bars, fetched_at, result["cached"] = await self.fetch(rt, symbol, limit, timeframe)
        except BrokerError as exc:
            result["error"] = exc.to_dict()
            result["message"] = f"Marktdaten nicht verfügbar: {exc.message}"
            return result
        result["fetched_at"] = iso(fetched_at)
        result["bars"] = [
            {"t": iso(b.t), "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v} for b in bars
        ]
        if not bars:
            result["status"] = "empty"
            result["message"] = (
                f"Keine Kerzen für {symbol}. Beim IEX-Feed fehlen Kerzen, wenn an IEX nicht gehandelt wurde."
                if rt.mode == "PAPER" and not is_crypto(symbol)
                else f"Keine Demo-Daten für {symbol}."
            )
            return result
        result["last_bar_at"] = iso(bars[-1].t)
        done = [b for b in bars if b.t + bar_len <= now]
        if done:
            result["last_complete_at"] = iso(done[-1].t)
            age = (now - (done[-1].t + bar_len)).total_seconds()
        else:
            age = None
        market_open = result["market_open"]
        if market_open is False:
            result["status"] = "market_closed"
            result["message"] = "Markt geschlossen – angezeigt werden die letzten verfügbaren Kerzen."
        elif age is None or age > max(max_age_s, 2 * bar_len.total_seconds()):
            result["status"] = "stale"
            mins = f"{age / 60:.0f} Min." if age is not None else "unbekannt"
            result["message"] = f"Daten veraltet (letzte abgeschlossene Kerze vor {mins})." + (
                " IEX deckt nur einen Teil des Marktes ab; bei wenig gehandelten Werten fehlen Minuten."
                if rt.mode == "PAPER" and not is_crypto(symbol)
                else ""
            )
        else:
            result["status"] = "ok"
            result["message"] = "Aktuell"
        result["age_s"] = age
        return result
