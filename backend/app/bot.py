"""Bot-Engine: genau eine Instanz, ausdrücklicher Start, Pause ohne Eingriff in Bestände.

* Start nur bei gültiger Konfiguration, erfolgreichem Kontoabgleich, geöffnetem
  Markt, aktivem Brokerstatus, frischen Daten und (PAPER) aktiver Orderfreigabe.
* Pause stoppt nur neue automatische Aufträge. Positionen und offene
  Broker-Orders bleiben unverändert bestehen.
* Jede abgeschlossene Minutenkerze wird höchstens einmal verarbeitet: Der
  Zeitstempel wird gespeichert, bevor eine Order gesendet wird, und die
  Kerze ist Teil des Idempotenzschlüssels der Bot-Order.
* Nach jedem Neustart ist die Automatik aus.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .broker.base import BrokerError, is_open_status
from .db import iso, parse_iso
from .orders import OrderBlocked, OrderFailed, OrderInput
from .risk import available_to_sell
from .scalper import QUOTE_MAX_AGE_S, ScalpEngine
from .settings_store import BotParams, get_bot_params
from .strategy import BAR_LENGTH, complete_bars, evaluate, session_bars

if TYPE_CHECKING:  # pragma: no cover
    from .core import AppContext

log = logging.getLogger("alpaca_bot.bot")


class BotStartError(Exception):
    pass


class BotEngine:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.mode: str | None = None
        self.last_tick_at = None
        self.last_status: str = ""
        self.strategy: str | None = None
        self.scalper = ScalpEngine(ctx)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode if self.running else None,
            "last_tick_at": iso(self.last_tick_at),
            "last_status": self.last_status,
            "strategy": self.strategy if self.running else None,
            "scalper": self.scalper.state if self.running and self.strategy == "scalp" else None,
            "pause_meaning": (
                "Pausieren stoppt nur neue automatische Aufträge. Bestehende Positionen bleiben bestehen, "
                "offene Broker-Orders bleiben aktiv. Stornieren/Schliessen sind separate Aktionen."
            ),
        }

    def _last_processed_key(self, symbol: str) -> str:
        return f"bot_last_bar:{symbol}"

    # -------------------------------------------------------------- start
    async def start(self, mode: str) -> dict:
        ctx = self.ctx
        async with self._lock:
            if self.running:
                raise BotStartError("Der Bot läuft bereits – es wird genau eine Instanz gestartet.")
            if mode != ctx.mode:
                raise BotStartError("Der Bot kann nur im aktiven Modus gestartet werden.")
            try:
                params = get_bot_params(ctx.db, mode)
            except ValidationError as exc:
                raise BotStartError(f"Ungültige Strategieparameter: {exc.errors()[0]['msg']}") from exc
            if mode == "PAPER" and not ctx.orders_enabled(mode):
                raise BotStartError("Paper-Orderfreigabe ist aus. Bitte zuerst die Freigabe ausdrücklich aktivieren.")
            rt = ctx.runtimes[mode]
            if not await rt.sync():
                err = rt.last_error
                raise BotStartError(f"Kontoabgleich fehlgeschlagen: {err.message if err else 'unbekannt'}")
            issues = rt.state_issues()
            if issues:
                raise BotStartError("Zustand nicht abgeglichen: " + "; ".join(issues))
            snap = rt.snapshot
            assert snap is not None
            acct = snap.account
            if acct.status != "ACTIVE" or acct.trading_blocked or acct.account_blocked or acct.trade_suspended_by_user:
                raise BotStartError(f"Broker-Handelsstatus erlaubt keinen Handel (Status {acct.status}).")
            if params.strategy == "scalp":
                await self._check_scalp_start(rt, params)
                self.scalper.reset(params)
                interval = params.poll_interval_s
                what = (
                    f"Scalping {', '.join(params.scalp_symbols)}: Mikrokerzen {params.bar_seconds} s, SMA{params.short_window}/"
                    f"SMA{params.long_window}, Einsatz {params.order_value:g} USD, Stop-Loss −{params.stop_loss_pct:g} %, "
                    f"Take-Profit +{params.take_profit_pct:g} %, max. {params.max_hold_min:g} Min., "
                    f"Kursabruf alle {params.poll_interval_s:g} s, Schutzorder beim Broker "
                    f"{'aktiv' if params.broker_stop else 'aus'}. Teststrategie ohne Gewinnzusage."
                )
            else:
                await self._check_sma_start(rt, params, snap)
                interval = ctx.settings.bot_interval_s
                what = (
                    f"{params.symbol}, SMA{params.short_window}/SMA{params.long_window} auf Minutenkerzen, "
                    f"Zielwert je Kauf {params.order_value:g}. Beispielstrategie zum Testen der Automatik."
                )
            self.mode = mode
            self.last_status = "Gestartet"
            self.strategy = params.strategy
            self._task = asyncio.create_task(self._run(mode, interval), name="alpaca-bot-loop")
            ctx.remember_run(mode=mode, bot=True)
            ctx.db.add_event(mode, "info", "bot", f"Bot gestartet: {what}", push=True)
            return self.state()

    async def _check_scalp_start(self, rt, params: BotParams) -> None:
        """Krypto: 24/7 – statt Börsenzeiten zählen handelbare Instrumente und frische Kurse."""
        symbols = params.scalp_symbols
        for symbol in symbols:
            try:
                asset = await rt.broker.get_asset(symbol)
            except BrokerError as exc:
                raise BotStartError(f"Instrument {symbol}: {exc.message}") from exc
            if not asset.tradable or asset.status != "active" or asset.asset_class != "crypto":
                raise BotStartError(f"Instrument {symbol} ist laut Broker nicht als Krypto handelbar.")
        try:
            quotes = await rt.broker.get_quotes(symbols)
        except BrokerError as exc:
            raise BotStartError(f"Kurse nicht verfügbar: {exc.message}") from exc
        now = rt._now()
        stale = [s for s in symbols if s not in quotes or (now - quotes[s].at).total_seconds() > QUOTE_MAX_AGE_S]
        if stale:
            raise BotStartError(f"Kein frischer Geld-/Briefkurs für {', '.join(stale)} (erlaubt {QUOTE_MAX_AGE_S:.0f} s).")
        if params.take_profit_pct <= params.roundtrip_cost_pct:
            self.ctx.db.add_event(
                self.ctx.mode, "warning", "bot",
                f"Hinweis: Take-Profit {params.take_profit_pct:g} % liegt nicht über den geschätzten Kosten "
                f"je Trade ({params.roundtrip_cost_pct:g} % Gebühren + Spread). Netto ist kaum Gewinn möglich.",
            )

    async def _check_sma_start(self, rt, params: BotParams, snap) -> None:
        ctx = self.ctx
        mode = rt.mode
        if not snap.clock.is_open:
            nxt = snap.clock.next_open
            raise BotStartError(
                "Markt ist geschlossen." + (f" Nächste Öffnung: {iso(nxt)}" if nxt else "")
            )
        try:
            asset = await rt.broker.get_asset(params.symbol)
        except BrokerError as exc:
            raise BotStartError(f"Instrument {params.symbol}: {exc.message}") from exc
        if not asset.tradable or asset.status != "active" or asset.asset_class != "us_equity":
            raise BotStartError(f"Instrument {params.symbol} ist laut Broker nicht handelbar.")
        try:
            bars, _, _ = await ctx.market.fetch(rt, params.symbol, self._bar_limit(params))
        except BrokerError as exc:
            raise BotStartError(f"Marktdaten nicht verfügbar: {exc.message}") from exc
        now = rt._now()
        done = complete_bars(bars, now)
        if not done:
            raise BotStartError("Keine abgeschlossenen Minutenkerzen verfügbar.")
        age = (now - (done[-1].t + BAR_LENGTH)).total_seconds()
        if age > params.max_bar_age_s:
            raise BotStartError(
                f"Daten nicht frisch: letzte abgeschlossene Kerze vor {age / 60:.1f} Min. "
                f"(erlaubt {params.max_bar_age_s / 60:.0f} Min.)."
            )
        if rt.broker.uses_sessions:
            try:
                if await rt.broker.get_session_open(now) is None:
                    raise BotStartError("Heute findet laut Handelskalender keine reguläre Sitzung statt.")
            except BrokerError as exc:
                raise BotStartError(f"Handelskalender nicht verfügbar: {exc.message}") from exc
        # Kerzen vor dem Start lösen keine Signale aus.
        ctx.db.set(mode, self._last_processed_key(params.symbol), iso(done[-1].t))


    async def pause(self, reason: str = "Vom Nutzer pausiert", *, remember: bool = True) -> dict:
        """``remember=False`` nur beim Herunterfahren: dann bleibt „Automatik lief“ für das Fortsetzen gemerkt."""
        async with self._lock:
            task, mode = self._task, self.mode
            self._task = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                if remember:
                    self.ctx.remember_run(bot=False)
                self.ctx.db.add_event(
                    mode or self.ctx.mode,
                    "info",
                    "bot",
                    f"Bot pausiert ({reason}). Positionen und offene Broker-Orders bleiben unverändert.",
                    push=True,
                )
            self.last_status = f"Pausiert: {reason}"
            return self.state()

    # -------------------------------------------------------------- loop
    @staticmethod
    def _bar_limit(params: BotParams) -> int:
        return max(params.long_window * 4, 120)

    async def _run(self, mode: str, interval: float) -> None:
        while True:
            try:
                await self.tick(mode)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Bot darf nicht still sterben
                log.exception("Bot-Tick fehlgeschlagen")
                self._status(mode, f"Interner Fehler: {exc}", level="error")
            await asyncio.sleep(interval)

    def _status(self, mode: str, text: str, level: str = "info") -> None:
        """Statuswechsel einmalig protokollieren (kein Spam bei gleichbleibendem Zustand)."""
        if text != self.last_status:
            self.ctx.db.add_event(mode, level, "bot", text)
        self.last_status = text

    async def tick(self, mode: str) -> None:
        ctx = self.ctx
        rt = ctx.runtimes[mode]
        self.last_tick_at = rt._now()
        if ctx.mode != mode:
            self._status(mode, "Modus gewechselt – Bot ohne Aktion")
            return
        params = get_bot_params(ctx.db, mode)
        if params.strategy == "scalp":
            await self.scalper.tick(mode, params)
            self.last_status = self.scalper.state.get("status", "Läuft")
            return
        await self._tick_sma(mode, params)

    async def _tick_sma(self, mode: str, params: BotParams) -> None:
        ctx = self.ctx
        rt = ctx.runtimes[mode]
        if not await rt.sync():
            err = rt.last_error
            self._status(mode, f"Getrennt: {err.message if err else 'unbekannt'} – keine neuen Aufträge", "warning")
            return
        snap = rt.snapshot
        assert snap is not None
        if not snap.clock.is_open:
            self._status(mode, "Markt geschlossen – keine neuen Aufträge")
            return
        try:
            bars, _, _ = await ctx.market.fetch(rt, params.symbol, self._bar_limit(params))
        except BrokerError as exc:
            self._status(mode, f"Marktdaten nicht verfügbar: {exc.message}", "warning")
            return
        now = rt._now()
        done = complete_bars(bars, now)
        if not done:
            self._status(mode, "Keine abgeschlossenen Kerzen")
            return
        session_open = None
        if rt.broker.uses_sessions:
            try:
                session_open = await rt.broker.get_session_open(now)
            except BrokerError as exc:
                self._status(mode, f"Handelskalender nicht verfügbar: {exc.message} – keine neuen Aufträge", "warning")
                return
            if session_open is None:
                self._status(mode, "Heute keine reguläre Handelssitzung – keine neuen Aufträge")
                return
        latest = done[-1]
        key = self._last_processed_key(params.symbol)
        last_processed = parse_iso(ctx.db.get(mode, key))
        if last_processed is not None and latest.t <= last_processed:
            return  # Kerze bereits verarbeitet
        # Vor jeder Aktion als verarbeitet markieren → höchstens einmal.
        ctx.db.set(mode, key, iso(latest.t))
        self._status(mode, "Läuft")

        age = (now - (latest.t + BAR_LENGTH)).total_seconds()
        if age > params.max_bar_age_s:
            ctx.db.add_decision(
                mode, symbol=params.symbol, bar_time=iso(latest.t), close=latest.c, signal="none",
                action="skip", reason=f"Daten veraltet ({age / 60:.1f} Min.) – keine Entscheidung",
            )
            return
        bars_in_session = session_bars(done, session_open)
        if not bars_in_session or bars_in_session[-1].t != latest.t:
            ctx.db.add_decision(
                mode, symbol=params.symbol, bar_time=iso(latest.t), close=latest.c, signal="none",
                action="skip", reason="Kerze liegt ausserhalb der regulären Handelssitzung – keine Entscheidung",
            )
            return
        needed = params.long_window + 1
        if session_open is not None and len(bars_in_session) < needed:
            ctx.db.add_decision(
                mode, symbol=params.symbol, bar_time=iso(latest.t), close=latest.c, signal="none",
                action="none",
                reason=f"Zu wenige Kerzen der laufenden Sitzung ({len(bars_in_session)}/{needed}) – "
                "Kerzen vom Vortag werden bewusst nicht verwendet",
            )
            return
        sig = evaluate(bars_in_session, params.short_window, params.long_window)
        base = dict(
            symbol=params.symbol,
            bar_time=iso(latest.t),
            close=sig.close,
            sma_short=round(sig.sma_short, 4) if sig.sma_short is not None else None,
            sma_long=round(sig.sma_long, 4) if sig.sma_long is not None else None,
            signal=sig.kind,
        )
        if sig.kind == "none":
            ctx.db.add_decision(mode, **base, action="none", reason=sig.reason)
            return

        has_position = any(p.symbol == params.symbol and p.qty > 0 for p in snap.positions)
        open_buy = any(
            o.symbol == params.symbol and o.side == "buy" and is_open_status(o.status) for o in snap.open_orders
        )
        if sig.kind == "buy":
            if has_position or open_buy:
                why = "Position vorhanden" if has_position else "offene Kauforder vorhanden"
                ctx.db.add_decision(mode, **base, action="skip", reason=f"{sig.reason}; kein Kauf: {why}")
                return
            try:
                asset = await rt.broker.get_asset(params.symbol)
            except BrokerError as exc:
                ctx.db.add_decision(mode, **base, action="skip", reason=f"{sig.reason}; Instrument: {exc.message}")
                return
            inp = self._buy_input(params, sig.close, asset.fractionable)
            if inp is None:
                ctx.db.add_decision(
                    mode, **base, action="skip",
                    reason=f"{sig.reason}; Zielwert {params.order_value:g} reicht nicht für 1 ganzes Stück",
                )
                return
        else:
            qty = available_to_sell(snap.positions, snap.open_orders, params.symbol)
            if qty <= 0:
                ctx.db.add_decision(mode, **base, action="skip", reason=f"{sig.reason}; kein verfügbarer Bestand")
                return
            inp = OrderInput(
                symbol=params.symbol,
                side="sell",
                qty=Decimal(str(qty)).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN).normalize(),
            )
        idem = f"bot:{params.symbol}:{iso(latest.t)}:{sig.kind}"
        try:
            res = await ctx.orders.submit(mode, inp, source="bot", idempotency_key=idem)
        except OrderBlocked as exc:
            failed = [c for c in exc.preview.get("checks", []) if c["blocking"] and not c["ok"]]
            why = "; ".join(f"{c['name']}: {c['detail']}" for c in failed) or "gesperrt"
            ctx.db.add_decision(mode, **base, action="blocked", reason=f"{sig.reason}; Order gesperrt – {why}")
            return
        except OrderFailed as exc:
            ctx.db.add_decision(mode, **base, action="failed", reason=f"{sig.reason}; Broker lehnte ab: {exc.error.message}")
            return
        order = res["order"]
        what = f"{order.get('qty') or ''} Stück" if order.get("qty") else f"Betrag {order.get('notional')}"
        ctx.db.add_decision(
            mode, **base, action="order",
            reason=f"{sig.reason}; Order übermittelt ({what}), Status {order.get('status')}",
        )

    @staticmethod
    def _buy_input(params: BotParams, price: float | None, fractionable: bool) -> OrderInput | None:
        """Bruchstückfähig: Betragsorder über den Zielwert. Sonst ganze Stück (abgerundet)."""
        if fractionable:
            return OrderInput(
                symbol=params.symbol,
                side="buy",
                notional=Decimal(str(params.order_value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN),
            )
        if not price:
            return None
        qty = int(params.order_value // price)
        return OrderInput(symbol=params.symbol, side="buy", qty=Decimal(qty)) if qty >= 1 else None
