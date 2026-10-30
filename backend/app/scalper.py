"""Scalping-Engine für Krypto (Teststrategie, keine Gewinnzusage).

Ablauf je Durchlauf (alle ``poll_interval_s`` Sekunden):
1. Besten Geld-/Briefkurs abrufen (nur data.alpaca.markets bzw. DEMO).
2. Mittelkurs in Mikrokerzen von ``bar_seconds`` einsortieren.
3. Läuft ein Trade: Ausstieg prüfen – Stop-Loss bzw. Take-Profit auf dem
   Geldkurs (zu diesem Kurs wird verkauft) oder maximale Haltedauer.
4. Sonst: Jede abgeschlossene Mikrokerze höchstens einmal auswerten; Einstieg
   nur bei einer Aufwärtskreuzung kurzer/langer Durchschnitt.

Alle Orders laufen über den gemeinsamen Orderweg (Risikoprüfung,
client_order_id, keine blinden Wiederholungen). Bei Pause werden keine neuen
Orders gesendet – auch keine Ausstiege; offene Positionen bleiben bestehen.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from .broker.base import FINAL_ORDER_STATUSES, LOCAL_FINAL, Bar, BrokerError, Order, Quote
from .db import iso, parse_iso
from .orders import OrderBlocked, OrderFailed, OrderInput
from .risk import available_to_sell
from .settings_store import BotParams, get_risk_limits
from .strategy import evaluate

if TYPE_CHECKING:  # pragma: no cover
    from .core import AppContext

QUOTE_MAX_AGE_S = 15.0  # ältere Kurse gelten als veraltet
EXIT_RETRY_S = 10.0  # Wartezeit nach abgelehntem Ausstieg
ACTIVE = ("entering", "open", "exiting")


@dataclass
class _Bucket:
    idx: int
    o: float
    h: float
    l: float  # noqa: E741
    c: float


class MicroBars:
    """Mikrokerzen aus abgefragten Kursen. Lücken (Verbindungsabbruch) setzen die Reihe zurück."""

    def __init__(self, seconds: int, keep: int = 600) -> None:
        self.seconds = seconds
        self.keep = keep
        self.completed: list[_Bucket] = []
        self.current: _Bucket | None = None
        self.last_sample: datetime | None = None
        self.resets = 0

    def add(self, at: datetime, price: float) -> None:
        if self.last_sample is not None and (at - self.last_sample).total_seconds() > 3 * self.seconds:
            self.completed.clear()
            self.current = None
            self.resets += 1
        if self.last_sample is not None and at < self.last_sample:
            return
        self.last_sample = at
        idx = int(at.timestamp() // self.seconds)
        if self.current is None or idx > self.current.idx:
            if self.current is not None:
                self.completed.append(self.current)
                del self.completed[: -self.keep]
            self.current = _Bucket(idx, price, price, price, price)
        else:
            cur = self.current
            cur.h, cur.l, cur.c = max(cur.h, price), min(cur.l, price), price

    def bars(self) -> list[Bar]:
        return [
            Bar(t=datetime.fromtimestamp(b.idx * self.seconds, tz=timezone.utc), o=b.o, h=b.h, l=b.l, c=b.c)
            for b in self.completed
        ]


def _q9(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN).normalize()


class ScalpEngine:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.bars: MicroBars | None = None
        self.last_processed: datetime | None = None
        self.state: dict = {}
        self._exit_retry_after: dict[int, datetime] = {}
        self._lock = asyncio.Lock()  # Bot-Durchlauf und Hintergrundabgleich nie gleichzeitig

    # ------------------------------------------------------------ helpers
    def reset(self, params: BotParams) -> None:
        self.bars = MicroBars(params.bar_seconds)
        self.last_processed = None
        self.state = {"symbol": params.symbol, "status": "Aufwärmen – sammle Mikrokerzen"}

    def _db(self):
        return self.ctx.db

    def active_trade(self, mode: str, symbol: str) -> dict | None:
        return self._db().query_one(
            "SELECT * FROM trades WHERE mode=? AND symbol=? AND status IN ('entering','open','exiting') "
            "ORDER BY id DESC LIMIT 1",
            (mode, symbol),
        )

    def _update(self, trade_id: int, **fields) -> None:
        fields["updated_at"] = iso(self.ctx._now())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._db().execute(f"UPDATE trades SET {cols} WHERE id=?", (*fields.values(), trade_id))

    def _decide(self, mode: str, params: BotParams, signal: str, action: str, reason: str, **extra) -> None:
        self._db().add_decision(mode, symbol=params.symbol, signal=signal, action=action, reason=reason, **extra)

    async def _lookup(self, mode: str, cid: str | None) -> Order | None:
        if not cid:
            return None
        rt = self.ctx.runtimes[mode]
        try:
            return await rt.broker.get_order_by_client_id(cid)
        except BrokerError as exc:
            self.state["status"] = f"Orderabfrage fehlgeschlagen: {exc.message}"
            return None

    def _local_order(self, mode: str, cid: str | None) -> dict | None:
        if not cid:
            return None
        return self._db().query_one("SELECT * FROM orders WHERE mode=? AND client_order_id=?", (mode, cid))

    # --------------------------------------------------------------- tick
    async def tick(self, mode: str, params: BotParams) -> None:
        async with self._lock:
            await self._tick(mode, params)

    async def finalize_all(self, mode: str) -> None:
        """Hintergrund (auch bei Pause): offene Ein-/Ausstiegsorders nur lesend abgleichen."""
        async with self._lock:
            now = self.ctx._now()
            for trade in self._db().query(
                "SELECT * FROM trades WHERE mode=? AND status IN ('entering','exiting')", (mode,)
            ):
                await self.finalize(mode, trade, now)

    async def _tick(self, mode: str, params: BotParams) -> None:
        if self.bars is None or self.bars.seconds != params.bar_seconds:
            self.reset(params)
        assert self.bars is not None
        rt = self.ctx.runtimes[mode]
        now = rt._now()
        quote: Quote | None
        try:
            quote = await rt.broker.get_quote(params.symbol)
        except BrokerError as exc:
            quote = None
            self.state["status"] = f"Kursabruf fehlgeschlagen ({exc.kind}): {exc.message}"
        fresh = quote is not None and (now - quote.at).total_seconds() <= QUOTE_MAX_AGE_S
        if quote is not None:
            self.state["quote"] = {
                "bid": quote.bid,
                "ask": quote.ask,
                "spread_pct": round(quote.spread_pct, 4),
                "at": iso(quote.at),
                "fresh": fresh,
                "source": quote.source,
            }
        if fresh:
            assert quote is not None
            self.bars.add(now, quote.mid)
        elif quote is not None:
            self.state["status"] = "Kurs veraltet – keine Einstiege"

        trade = self.active_trade(mode, params.symbol)
        if trade is not None:
            await self._manage(mode, params, trade, quote if fresh else None, now)
            return
        self.state.pop("trade", None)
        await self._maybe_enter(mode, params, quote if fresh else None, now)

    # ------------------------------------------------------------ Einstieg
    async def _maybe_enter(self, mode: str, params: BotParams, quote: Quote | None, now: datetime) -> None:
        assert self.bars is not None
        bars = self.bars.bars()
        needed = params.long_window + 1
        self.state["bars_ready"] = min(len(bars), needed)
        self.state["bars_needed"] = needed
        if not bars:
            return
        latest = bars[-1]
        if self.last_processed is not None and latest.t <= self.last_processed:
            return
        self.last_processed = latest.t  # jede Mikrokerze höchstens einmal
        if len(bars) < needed:
            self.state["status"] = f"Aufwärmen: {len(bars)}/{needed} Mikrokerzen à {params.bar_seconds} s"
            return
        sig = evaluate(bars, params.short_window, params.long_window)
        self.state["last_eval"] = {
            "bar_time": iso(latest.t),
            "close": latest.c,
            "sma_short": sig.sma_short,
            "sma_long": sig.sma_long,
            "signal": sig.kind,
        }
        if sig.kind != "buy":
            self.state["status"] = f"Beobachte – {sig.reason}"
            return
        extra = dict(
            bar_time=iso(latest.t),
            close=latest.c,
            sma_short=round(sig.sma_short or 0, 4),
            sma_long=round(sig.sma_long or 0, 4),
        )
        if quote is None:
            self._decide(mode, params, "buy", "skip", f"{sig.reason}; kein frischer Kurs – kein Einstieg", **extra)
            return
        inp = OrderInput(
            symbol=params.symbol,
            side="buy",
            notional=Decimal(str(params.order_value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN),
            time_in_force="gtc",
        )
        idem = f"scalp:{params.symbol}:{iso(latest.t)}"
        try:
            res = await self.ctx.orders.submit(mode, inp, source="bot", idempotency_key=idem)
        except OrderBlocked as exc:
            failed = [c for c in exc.preview.get("checks", []) if c["blocking"] and not c["ok"]]
            why = "; ".join(f"{c['name']}: {c['detail']}" for c in failed) or "gesperrt"
            self._decide(mode, params, "buy", "blocked", f"{sig.reason}; Einstieg gesperrt – {why}", **extra)
            self.state["status"] = f"Einstieg gesperrt – {why}"
            return
        except OrderFailed as exc:
            self._decide(mode, params, "buy", "failed", f"{sig.reason}; Broker lehnte ab: {exc.error.message}", **extra)
            return
        if res.get("duplicate"):
            return
        order = res["order"]
        ts = iso(now)
        self._db().execute(
            "INSERT INTO trades (mode, symbol, status, entry_cid, fee_pct, created_at, updated_at) "
            "VALUES (?, ?, 'entering', ?, ?, ?, ?)",
            (mode, params.symbol, order["client_order_id"], params.fee_pct, ts, ts),
        )
        self._decide(
            mode, params, "buy", "order",
            f"{sig.reason}; Einstieg {params.order_value:g} USD gesendet (Spread {quote.spread_pct:.3f} %)",
            **extra,
        )
        self.state["status"] = "Einstieg gesendet – warte auf Ausführung"

    # ------------------------------------------------------------ Trades
    async def _manage(self, mode: str, params: BotParams, trade: dict, quote: Quote | None, now: datetime) -> None:
        if trade["status"] in ("entering", "exiting"):
            await self.finalize(mode, trade, now)
            trade = self._db().query_one("SELECT * FROM trades WHERE id=?", (trade["id"],)) or trade
            if trade["status"] != "open":
                return
        await self._check_exit(mode, params, trade, quote, now)

    async def finalize(self, mode: str, trade: dict, now: datetime) -> None:
        """Nur lesend: Ein-/Ausstiegsorders abgleichen (läuft auch bei pausiertem Bot)."""
        if trade["status"] == "entering":
            order = await self._lookup(mode, trade["entry_cid"])
            if order is None:
                local = self._local_order(mode, trade["entry_cid"])
                if local and local["status"] in LOCAL_FINAL:
                    self._update(trade["id"], status="canceled", note=local.get("message") or "Einstieg nicht ausgeführt")
                return
            if order.filled_qty > 0 and (order.status == "filled" or order.status in FINAL_ORDER_STATUSES):
                self._open_trade(mode, trade, order, now)
            elif order.status in FINAL_ORDER_STATUSES:
                self._update(trade["id"], status="canceled", note=f"Einstieg {order.status}, nichts ausgeführt")
                self._db().add_event(mode, "warning", "trade", f"Einstieg {trade['symbol']} {order.status} – kein Trade.")
        elif trade["status"] == "exiting":
            order = await self._lookup(mode, trade["exit_cid"])
            if order is None:
                local = self._local_order(mode, trade["exit_cid"])
                if local and local["status"] in LOCAL_FINAL:
                    self._update(trade["id"], status="open", exit_cid=None, note="Ausstieg nicht ausgeführt – erneuter Versuch")
                return
            if order.filled_qty > 0 and order.status in FINAL_ORDER_STATUSES:
                self._close_trade(mode, trade, order, now)
            elif order.status in FINAL_ORDER_STATUSES:
                self._update(trade["id"], status="open", exit_cid=None, note=f"Ausstieg {order.status} – erneuter Versuch")

    def _open_trade(self, mode: str, trade: dict, order: Order, now: datetime) -> None:
        params = BotParams(**self._db().get(mode, "bot_params", {}))
        entry = order.filled_avg_price or 0.0
        entry_time = order.filled_at or now
        self._update(
            trade["id"],
            status="open",
            entry_time=iso(entry_time),
            entry_qty=order.filled_qty,
            entry_price=entry,
            stop_price=round(entry * (1 - params.stop_loss_pct / 100), 6),
            target_price=round(entry * (1 + params.take_profit_pct / 100), 6),
            deadline=iso(entry_time + timedelta(minutes=params.max_hold_min)),
        )
        self._db().add_event(
            mode, "info", "trade",
            f"Einstieg ausgeführt: {order.filled_qty:g} {trade['symbol']} zu {entry:.2f}. "
            f"Stop-Loss −{params.stop_loss_pct:g} %, Take-Profit +{params.take_profit_pct:g} %, "
            f"max. {params.max_hold_min:g} Min.",
        )

    def _close_trade(self, mode: str, trade: dict, order: Order, now: datetime) -> None:
        exit_price = order.filled_avg_price or 0.0
        qty = order.filled_qty
        entry = trade["entry_price"] or 0.0
        fee = (trade["fee_pct"] or 0.0) / 100
        gross = (exit_price - entry) * qty
        fees = fee * (entry * (trade["entry_qty"] or qty) + exit_price * qty)
        net = gross - fees
        self._update(
            trade["id"],
            status="closed",
            exit_time=iso(order.filled_at or now),
            exit_qty=qty,
            exit_price=exit_price,
            gross_pnl=round(gross, 4),
            fees_est=round(fees, 4),
            net_pnl=round(net, 4),
        )
        self._db().add_event(
            mode, "info" if net >= 0 else "warning", "trade",
            f"Trade {trade['symbol']} geschlossen ({trade['exit_reason']}): brutto {gross:+.2f}, "
            f"Kosten ~{fees:.2f}, netto {net:+.2f} USD",
        )
        self._check_loss_streak(mode, trade["id"], now)

    def _check_loss_streak(self, mode: str, trade_id: int, now: datetime) -> None:
        limits = get_risk_limits(self._db(), mode)
        since = int(self._db().get(mode, "loss_streak_since", 0) or 0)
        rows = self._db().query(
            "SELECT net_pnl FROM trades WHERE mode=? AND status='closed' AND id>? AND net_pnl IS NOT NULL "
            "ORDER BY id DESC",
            (mode, since),
        )
        streak = 0
        for r in rows:
            if r["net_pnl"] < 0:
                streak += 1
            else:
                break
        if streak >= limits.loss_streak_limit:
            until = now + timedelta(minutes=limits.loss_pause_min)
            self._db().set(mode, "loss_pause", {"until": iso(until), "reason": f"{streak} Verlust-Trades in Folge"})
            self._db().set(mode, "loss_streak_since", trade_id)
            self._db().add_event(
                mode, "warning", "risk",
                f"{streak} Verlust-Trades in Folge – neue Käufe pausiert für {limits.loss_pause_min} Min. "
                "(Verkäufe bleiben möglich).",
            )

    async def _check_exit(self, mode: str, params: BotParams, trade: dict, quote: Quote | None, now: datetime) -> None:
        entry = trade["entry_price"] or 0.0
        deadline = parse_iso(trade["deadline"])
        info = {
            "id": trade["id"],
            "qty": trade["entry_qty"],
            "entry_price": entry,
            "stop_price": trade["stop_price"],
            "target_price": trade["target_price"],
            "deadline": trade["deadline"],
            "entry_time": trade["entry_time"],
        }
        reason = None
        if quote is not None and entry > 0:
            info["bid"] = quote.bid
            info["pnl_pct"] = round((quote.bid / entry - 1) * 100, 4)
            if quote.bid <= (trade["stop_price"] or 0):
                reason = "Stop-Loss"
            elif quote.bid >= (trade["target_price"] or float("inf")):
                reason = "Take-Profit"
        if reason is None and deadline is not None and now >= deadline:
            reason = "Max. Haltedauer"
        # Position extern geschlossen (z. B. manuell oder Not-Aus)?
        snap = self.ctx.runtimes[mode].snapshot
        entry_time = parse_iso(trade["entry_time"])
        if (
            snap is not None
            and entry_time is not None
            and snap.fetched_at > entry_time + timedelta(seconds=5)
            and not any(p.symbol == trade["symbol"] and p.qty > 0 for p in snap.positions)
        ):
            self._update(trade["id"], status="closed", exit_reason="extern geschlossen", exit_time=iso(now),
                         note="Position beim Broker nicht mehr vorhanden – Ergebnis siehe Auftragstabelle")
            self._db().add_event(mode, "info", "trade", f"Trade {trade['symbol']}: Position extern geschlossen.")
            return
        self.state["trade"] = info
        if reason is None:
            self.state["status"] = "Position offen – überwache Stop-Loss/Take-Profit/Haltedauer"
            return
        retry = self._exit_retry_after.get(trade["id"])
        if retry is not None and now < retry:
            return
        await self._exit(mode, params, trade, reason, quote, now)

    async def _exit(self, mode: str, params: BotParams, trade: dict, reason: str, quote: Quote | None, now: datetime) -> None:
        rt = self.ctx.runtimes[mode]
        if not await rt.sync() or rt.snapshot is None:
            self.state["status"] = "Ausstieg verschoben – Brokerzustand nicht abrufbar"
            self._exit_retry_after[trade["id"]] = now + timedelta(seconds=EXIT_RETRY_S)
            return
        available = available_to_sell(rt.snapshot.positions, rt.snapshot.open_orders, trade["symbol"])
        if available <= 0:
            self._update(trade["id"], status="closed", exit_reason="extern geschlossen", exit_time=iso(now),
                         note="Kein verfügbarer Bestand mehr")
            return
        # Gebühren können den Bestand leicht verringern → höchstens den verfügbaren Bestand verkaufen.
        qty = _q9(min(trade["entry_qty"] or available, available))
        inp = OrderInput(symbol=trade["symbol"], side="sell", qty=qty, time_in_force="gtc")
        attempt = int(trade["exit_attempts"] or 0)
        idem = f"exit:{trade['id']}:{attempt}"
        bid = f" (Geldkurs {quote.bid:.2f})" if quote else ""
        try:
            res = await self.ctx.orders.submit(mode, inp, source="bot", idempotency_key=idem)
        except OrderBlocked as exc:
            failed = [c for c in exc.preview.get("checks", []) if c["blocking"] and not c["ok"]]
            why = "; ".join(f"{c['name']}: {c['detail']}" for c in failed) or "gesperrt"
            self._decide(mode, params, "sell", "blocked", f"{reason}{bid}; Ausstieg gesperrt – {why}")
            self._exit_retry_after[trade["id"]] = now + timedelta(seconds=EXIT_RETRY_S)
            return
        except OrderFailed as exc:
            self._update(trade["id"], exit_attempts=attempt + 1)
            self._decide(mode, params, "sell", "failed", f"{reason}{bid}; Broker lehnte Ausstieg ab: {exc.error.message}")
            self._exit_retry_after[trade["id"]] = now + timedelta(seconds=EXIT_RETRY_S)
            return
        order = res["order"]
        self._update(trade["id"], status="exiting", exit_cid=order["client_order_id"], exit_reason=reason,
                     exit_attempts=attempt + 1)
        self._decide(mode, params, "sell", "order", f"{reason}{bid}; Verkauf {qty} gesendet")
        self.state["status"] = f"Ausstieg ({reason}) gesendet – warte auf Ausführung"

    # ------------------------------------------------------------ Anzeige
    def summary(self, mode: str) -> dict:
        from .timeutil import trading_day

        today = trading_day(self.ctx.runtimes[mode]._now())
        rows = self._db().query(
            "SELECT net_pnl, created_at FROM trades WHERE mode=? AND status='closed'", (mode,)
        )
        todays = [r for r in rows if r["created_at"] and trading_day(parse_iso(r["created_at"])) == today]
        known = [r["net_pnl"] for r in todays if r["net_pnl"] is not None]
        return {
            "trades_today": len(todays),
            "wins_today": sum(1 for v in known if v >= 0),
            "losses_today": sum(1 for v in known if v < 0),
            "net_today": round(sum(known), 2),
            "loss_pause": self.ctx.risk.loss_pause(mode),
        }

    def list_trades(self, mode: str, limit: int = 50) -> list[dict]:
        return self._db().query("SELECT * FROM trades WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))
