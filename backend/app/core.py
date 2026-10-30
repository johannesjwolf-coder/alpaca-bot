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
from .db import iso
from .marketdata import MarketDataService
from .notify import Notifier
from .orders import OrderService
from .risk import RiskEngine
from .runtime import ModeRuntime
from .settings_store import APP_SCOPE
from .streams import Connector, StreamManager
from .symbols import SymbolService
from .timeutil import utcnow

log = logging.getLogger("alpaca_bot.core")

RESUME_ENABLED_KEY = "resume_after_restart"
RESUME_STATE_KEY = "resume_state"
RESUME_ATTEMPTS = 10
RESUME_RETRY_S = 30.0


class AppContext:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        paper_broker: Broker | None = None,
        demo_broker: Broker | None = None,
        now: Callable[[], datetime] = utcnow,
        stream_connector: Connector | None = None,
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
        self.streams = StreamManager(self, connector=stream_connector)
        self.notifier = Notifier(self, now=now)
        db.event_listeners.append(self.notifier.on_event)
        self._sync_task: asyncio.Task | None = None
        self._resume_task: asyncio.Task | None = None
        self.resume_status: str | None = None
        self._mode_lock = asyncio.Lock()
        self.started_at = now()

    # -------------------------------------------------------- Freigabe
    def orders_enabled(self, mode: str) -> bool:
        return bool(self.db.get(mode, "orders_enabled", False))

    def set_orders_enabled(self, mode: str, enabled: bool, note: str = "") -> None:
        self.db.set(mode, "orders_enabled", bool(enabled))
        if mode == "PAPER":
            self.remember_run(paper_release=bool(enabled))
        self.db.add_event(
            mode,
            "warning" if enabled else "info",
            "release",
            ("Paper-Orderfreigabe AKTIVIERT – manuelle und automatische Paper-Orders sind möglich."
             if enabled
             else "Paper-Orderfreigabe deaktiviert – Paper-Orders sind gesperrt.") + (f" {note}" if note else ""),
        )

    # ------------------------------------------- Fortsetzen nach Neustart
    def resume_enabled(self) -> bool:
        return bool(self.db.get(APP_SCOPE, RESUME_ENABLED_KEY, False))

    def set_resume_enabled(self, enabled: bool) -> None:
        self.db.set(APP_SCOPE, RESUME_ENABLED_KEY, bool(enabled))
        self.db.add_event(
            self.mode,
            "warning" if enabled else "info",
            "settings",
            "Fortsetzen nach Neustart AKTIVIERT: Läuft die Automatik beim Neustart, werden Orderfreigabe und Bot "
            "automatisch wiederhergestellt." if enabled
            else "Fortsetzen nach Neustart deaktiviert: Automatik und Orderfreigabe sind nach jedem Neustart aus.",
        )

    def resume_state(self) -> dict:
        return self.db.get(APP_SCOPE, RESUME_STATE_KEY, {}) or {}

    def remember_run(self, **changes: object) -> None:
        """Merkt sich, ob die Automatik läuft – Grundlage für das optionale Fortsetzen."""
        state = self.resume_state()
        state.update(changes, updated_at=iso(self._now()))
        self.db.set(APP_SCOPE, RESUME_STATE_KEY, state)

    def resume_info(self) -> dict:
        state = self.resume_state()
        return {
            "enabled": self.resume_enabled(),
            "last_run": {"mode": state.get("mode"), "bot": bool(state.get("bot")), "updated_at": state.get("updated_at")},
            "status": self.resume_status,
            "pending": self._resume_task is not None and not self._resume_task.done(),
        }

    async def resume_after_restart(self, state: dict, retry_s: float = RESUME_RETRY_S) -> bool:
        """Stellt Modus, Paper-Orderfreigabe und Bot wieder her (nur wenn ausdrücklich aktiviert)."""
        from .bot import BotStartError

        mode = state.get("mode")
        if mode not in APP_MODES:
            return False
        self.db.add_event(mode, "warning", "system",
                          f"Fortsetzen nach Neustart: Automatik lief zuletzt im Modus {mode} – Wiederherstellung startet.",
                          push=True)
        last_error = "unbekannt"
        for attempt in range(1, RESUME_ATTEMPTS + 1):
            if not self.resume_enabled():
                self.resume_status = "Abgebrochen: Fortsetzen wurde deaktiviert."
                return False
            if self.bot.running:
                self.resume_status = "Bot läuft bereits – nichts zu tun."
                return True
            self.resume_status = f"Versuch {attempt}/{RESUME_ATTEMPTS} …"
            try:
                if self.mode != mode:
                    await self.set_mode(mode)
                if mode == "PAPER" and not self.orders_enabled(mode):
                    self.set_orders_enabled(mode, True, "(Fortsetzen nach Neustart)")
                await self.bot.start(mode)
            except BotStartError as exc:
                last_error = str(exc)
                if "läuft bereits" in last_error:
                    self.resume_status = "Bot läuft bereits – nichts zu tun."
                    return True
                log.warning("Fortsetzen nach Neustart: Versuch %s fehlgeschlagen: %s", attempt, last_error)
                if attempt < RESUME_ATTEMPTS:
                    await asyncio.sleep(retry_s)
                continue
            self.resume_status = f"Fortgesetzt (Versuch {attempt})."
            self.db.add_event(mode, "warning", "bot", "Automatik nach Neustart automatisch fortgesetzt.", push=True)
            return True
        if mode == "PAPER" and self.orders_enabled(mode):
            self.set_orders_enabled(mode, False, "(Fortsetzen nach Neustart fehlgeschlagen)")
        self.resume_status = f"Fehlgeschlagen: {last_error}"
        self.db.add_event(mode, "error", "system",
                          f"Fortsetzen nach Neustart fehlgeschlagen nach {RESUME_ATTEMPTS} Versuchen: {last_error}. "
                          "Automatik und Orderfreigabe bleiben aus.", push=True)
        return False

    @property
    def runtime(self) -> ModeRuntime:
        return self.runtimes[self.mode]

    # ------------------------------------------------------- Lebenszyklus
    async def startup(self, background: bool = True) -> None:
        for m in APP_MODES:
            self.db.set(m, "orders_enabled", False)
            self.db.set(m, "bot_running", False)
        state = self.resume_state()
        resume = self.resume_enabled() and bool(state.get("bot")) and state.get("mode") in APP_MODES
        self.db.add_event(
            self.mode,
            "info",
            "system",
            f"Anwendung gestartet im Modus {self.mode}. "
            + ("Fortsetzen nach Neustart ist aktiv – die Automatik wird wiederhergestellt."
               if resume else "Automatik und Paper-Orderfreigabe sind nach dem Neustart aus."),
            push=True,
        )
        if background:
            self.notifier.start()
            await self.streams.orders_for(self.mode)
            self._sync_task = asyncio.create_task(self._sync_loop(), name="broker-sync")
            if resume:
                self._resume_task = asyncio.create_task(self.resume_after_restart(state), name="resume")

    async def shutdown(self) -> None:
        if self._resume_task and not self._resume_task.done():
            self._resume_task.cancel()
        # Beim Herunterfahren bleibt der gemerkte Zustand erhalten (Grundlage fürs Fortsetzen).
        await self.bot.pause("Anwendung wird beendet", remember=False)
        await self.streams.stop()
        await self.notifier.stop()
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
            await self.streams.orders_for(mode)
            self.db.add_event(mode, "info", "system", f"Modus gewechselt: {old} → {mode}. Orderfreigabe ist aus.")
            self.db.add_event(old, "info", "system", f"Modus gewechselt: {old} → {mode}.")
