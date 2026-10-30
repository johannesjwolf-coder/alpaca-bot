"""Anwendungskontext: Modi, Laufzeiten, Dienste und Hintergrundabgleich."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from .bot import BotEngine
from .broker.alpaca import AlpacaPaperBroker
from .broker.base import Broker
from .broker.demo import DemoBroker
from .config import APP_MODES, Settings
from .db import Database
from .marketdata import MarketDataService
from .orders import OrderService
from .risk import RiskEngine
from .runtime import ModeRuntime
from .symbols import SymbolService
from .timeutil import utcnow

log = logging.getLogger("alpaca_bot.core")


class AppContext:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        paper_broker: Broker | None = None,
        demo_broker: Broker | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.db = db
        self._now = now
        self.runtimes: dict[str, ModeRuntime] = {
            "DEMO": ModeRuntime("DEMO", demo_broker or DemoBroker(db, now=now), db, now=now),
            "PAPER": ModeRuntime("PAPER", paper_broker or AlpacaPaperBroker(), db, now=now),
        }
        self.mode = settings.start_mode
        self.market = MarketDataService(now=now)
        self.risk = RiskEngine(db, now=now)
        self.orders = OrderService(self)
        self.bot = BotEngine(self)
        self.symbols = SymbolService(self)
        self._sync_task: asyncio.Task | None = None
        self._mode_lock = asyncio.Lock()
        self.started_at = now()

    # -------------------------------------------------------- Freigabe
    def orders_enabled(self, mode: str) -> bool:
        return bool(self.db.get(mode, "orders_enabled", False))

    def set_orders_enabled(self, mode: str, enabled: bool) -> None:
        self.db.set(mode, "orders_enabled", bool(enabled))
        self.db.add_event(
            mode,
            "warning" if enabled else "info",
            "release",
            "Paper-Orderfreigabe AKTIVIERT – manuelle und automatische Paper-Orders sind möglich."
            if enabled
            else "Paper-Orderfreigabe deaktiviert – Paper-Orders sind gesperrt.",
        )

    @property
    def runtime(self) -> ModeRuntime:
        return self.runtimes[self.mode]

    # ------------------------------------------------------- Lebenszyklus
    async def startup(self, background: bool = True) -> None:
        for m in APP_MODES:
            self.db.set(m, "orders_enabled", False)
            self.db.set(m, "bot_running", False)
        self.db.add_event(
            self.mode,
            "info",
            "system",
            f"Anwendung gestartet im Modus {self.mode}. Automatik und Paper-Orderfreigabe sind nach dem Neustart aus.",
        )
        if background:
            self._sync_task = asyncio.create_task(self._sync_loop(), name="broker-sync")

    async def shutdown(self) -> None:
        await self.bot.pause("Anwendung wird beendet")
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except (asyncio.CancelledError, Exception):
                pass
        for rt in self.runtimes.values():
            await rt.broker.aclose()

    async def _sync_loop(self) -> None:
        # Regelmässiger Abgleich nur für den aktiven Modus (schont API-Limits).
        while True:
            try:
                if await self.runtime.sync(max_age_s=self.settings.sync_interval_s / 2):
                    await self.bot.scalper.finalize_all(self.mode)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Hintergrundabgleich fehlgeschlagen")
            await asyncio.sleep(self.settings.sync_interval_s)

    async def set_mode(self, mode: str) -> None:
        if mode not in APP_MODES:
            raise ValueError("Unbekannter Modus")
        async with self._mode_lock:
            if mode == self.mode:
                return
            old = self.mode
            await self.bot.pause(f"Moduswechsel {old} → {mode}")
            for m in APP_MODES:
                self.db.set(m, "orders_enabled", False)
            self.mode = mode
            self.db.add_event(mode, "info", "system", f"Modus gewechselt: {old} → {mode}. Orderfreigabe ist aus.")
            self.db.add_event(old, "info", "system", f"Modus gewechselt: {old} → {mode}.")
