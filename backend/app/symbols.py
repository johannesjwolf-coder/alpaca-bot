"""Symbolvorschläge für Chart, Orderformular und Bot.

Reihenfolge: Bot-Symbol, Bestände, zuletzt gehandelte Werte, beliebte Werte und –
bei einer Suche – passende handelbare Instrumente des Brokers (Liste 12 h im Cache).
DEMO schlägt nur die simulierten Instrumente vor.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .broker.base import Asset, BrokerError, is_crypto
from .settings_store import get_bot_params

if TYPE_CHECKING:  # pragma: no cover
    from .core import AppContext

POPULAR: list[tuple[str, str]] = [
    ("BTC/USD", "Bitcoin"),
    ("ETH/USD", "Ether"),
    ("SOL/USD", "Solana"),
    ("SPY", "SPDR S&P 500 ETF"),
    ("QQQ", "Invesco QQQ (Nasdaq-100)"),
    ("IWM", "iShares Russell 2000 ETF"),
    ("AAPL", "Apple"),
    ("MSFT", "Microsoft"),
    ("NVDA", "NVIDIA"),
    ("TSLA", "Tesla"),
]
CACHE_TTL = timedelta(hours=12)


def _kind(symbol: str) -> str:
    return "crypto" if is_crypto(symbol) else "us_equity"


class SymbolService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._assets: dict[str, tuple[datetime, list[Asset]]] = {}

    async def _asset_list(self, mode: str) -> tuple[list[Asset], str | None]:
        now = self.ctx._now()
        cached = self._assets.get(mode)
        if cached and now - cached[0] < CACHE_TTL:
            return cached[1], None
        broker = self.ctx.runtimes[mode].broker
        try:
            assets = [*await broker.list_assets("crypto"), *await broker.list_assets("us_equity")]
        except BrokerError as exc:
            return (cached[1] if cached else []), f"Instrumentsuche nicht verfügbar: {exc.message}"
        self._assets[mode] = (now, assets)
        return assets, None

    async def suggest(self, mode: str, q: str = "", limit: int = 24) -> dict:
        q = q.strip().upper()
        rt = self.ctx.runtimes[mode]
        items: list[dict] = []
        seen: set[str] = set()

        def add(symbol: str, name: str, group: str) -> None:
            if symbol in seen:
                return
            if q and not (symbol.startswith(q) or q in symbol.replace("/", "") or q in name.upper()):
                return
            seen.add(symbol)
            items.append({"symbol": symbol, "name": name, "asset_class": _kind(symbol), "group": group})

        names: dict[str, str] = {s: n for s, n in POPULAR}
        demo_symbols: set[str] | None = None
        if mode == "DEMO":
            demo_assets, _ = await self._asset_list(mode)
            demo_symbols = {a.symbol for a in demo_assets}
            names.update({a.symbol: a.name for a in demo_assets})

        try:
            bot_symbol = get_bot_params(self.ctx.db, mode).symbol
            add(bot_symbol, names.get(bot_symbol, ""), "Bot")
        except Exception:  # noqa: BLE001 - ungültige gespeicherte Parameter blockieren keine Vorschläge
            pass
        if rt.snapshot is not None:
            for p in rt.snapshot.positions:
                add(p.symbol, names.get(p.symbol, ""), "Im Depot")
        for row in self.ctx.db.query(
            "SELECT symbol, MAX(id) AS last FROM orders WHERE mode=? GROUP BY symbol ORDER BY last DESC LIMIT 6", (mode,)
        ):
            add(row["symbol"], names.get(row["symbol"], ""), "Zuletzt gehandelt")
        for symbol, name in POPULAR:
            if demo_symbols is None or symbol in demo_symbols:
                add(symbol, name, "Beliebt")
        if demo_symbols is not None:
            for symbol in sorted(demo_symbols):
                add(symbol, names.get(symbol, ""), "DEMO-Instrumente")

        note = None
        if q and mode == "PAPER":
            assets, note = await self._asset_list(mode)
            ranked = sorted(
                (a for a in assets if a.symbol.startswith(q) or q in a.name.upper()),
                key=lambda a: (not a.symbol.startswith(q), len(a.symbol), a.symbol),
            )
            for a in ranked[: limit * 2]:
                add(a.symbol, a.name, "Suche")
        return {"mode": mode, "query": q, "items": items[:limit], "note": note}
