"""Scalping-Engine für Krypto und US-Aktien/ETFs (Teststrategie, keine Gewinnzusage).

Ablauf je Durchlauf (alle ``poll_interval_s`` Sekunden bzw. bei neuen Kursen, für alle Symbole):
1. Beste Geld-/Briefkurse holen: Krypto per Datenstrom oder Abfrage, Aktien/ETFs per
   Abfrage (IEX) – Aktien/ETFs nur, solange die US-Börse offen ist.
2. Mittelkurs je Symbol in Mikrokerzen von ``bar_seconds`` einsortieren.
3. Läuft ein Trade: Nach dem Einstieg eine Stop-Limit-Schutzorder beim Broker
   hinterlegen (optional). Ausstieg bei Take-Profit oder maximaler Haltedauer:
   erst Schutzorder stornieren, dann verkaufen. Beim Stop-Loss greift die
   Schutzorder; bleibt sie unausgeführt, verkauft der Bot nach kurzer Frist selbst.
4. Sonst: Jede abgeschlossene Mikrokerze höchstens einmal auswerten; Einstieg
   nur bei einer Aufwärtskreuzung kurzer/langer Durchschnitt und (optional) nur,
   wenn der Kurs über einem steigenden Durchschnitt der Minutenkerzen liegt.
5. Aktien/ETFs: keine Einstiege in den letzten Minuten vor Börsenschluss, offene
   Trades kurz vor Schluss glattstellen (nie über Nacht). Krypto auf Wunsch nur,
   solange die US-Börse geschlossen ist. Schutzorder beim Broker nur für Krypto.

Alle Orders laufen über den gemeinsamen Orderweg (Risikoprüfung,
client_order_id, keine blinden Wiederholungen). Bei Pause sendet der Bot keine
neuen Orders – auch keine Ausstiege; eine hinterlegte Schutzorder bleibt beim
Broker aktiv, und der Hintergrundabgleich erfasst ihre Ausführung (nur lesend).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

from .broker.base import FINAL_ORDER_STATUSES, LOCAL_FINAL, Bar, BrokerError, Order, Quote, is_crypto, is_open_status
from .db import iso, parse_iso
from .orders import OrderBlocked, OrderFailed, OrderInput
from .risk import available_to_sell
from .settings_store import (
    FLATTEN_BEFORE_CLOSE_MIN,
    NO_ENTRY_BEFORE_CLOSE_MIN,
    BotParams,
    get_bot_params,
    get_risk_limits,
)
from .strategy import TREND_SLOPE_BARS, evaluate, trend_ok

if TYPE_CHECKING:  # pragma: no cover
    from .core import AppContext

QUOTE_MAX_AGE_S = 15.0  # ältere Kurse gelten als veraltet
EXIT_RETRY_S = 10.0  # Wartezeit nach abgelehntem Ausstieg
STOP_RETRY_S = 15.0  # Wartezeit nach abgelehnter Schutzorder
STOP_GRACE_S = 20.0  # so lange bekommt die Schutzorder beim Stop-Loss Zeit
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


@dataclass
class _SymState:
    bars: MicroBars
    last_processed: datetime | None = None
    view: dict = field(default_factory=dict)  # Anzeige im Frontend


def _q9(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN).normalize()


def round_down_price(price: float, increment: float | None) -> Decimal:
    """Preis auf das Preisraster des Instruments abrunden (Standard: Cent bzw. 6 Stellen)."""
    inc = Decimal(str(increment)) if increment else (Decimal("0.01") if price >= 1 else Decimal("0.000001"))
    return ((Decimal(str(price)) / inc).to_integral_value(rounding=ROUND_FLOOR) * inc).normalize()


class ScalpEngine:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.syms: dict[str, _SymState] = {}
        self.state: dict = {}
        self._exit_retry_after: dict[int, datetime] = {}
        self._stop_retry_after: dict[int, datetime] = {}
        self._lock = asyncio.Lock()  # Bot-Durchlauf und Hintergrundabgleich nie gleichzeitig

    # ------------------------------------------------------------ helpers
    def reset(self, params: BotParams) -> None:
        self.syms = {s: _SymState(MicroBars(params.bar_seconds)) for s in params.scalp_symbols}
        self.state = {
            "status": "Aufwärmen – sammle Mikrokerzen",
            "symbols": {s: st.view for s, st in self.syms.items()},
        }
        for st in self.syms.values():
            st.view.update(status="Aufwärmen")

    def _db(self):
        return self.ctx.db

    def active_trade(self, mode: str, symbol: str) -> dict | None:
        return self._db().query_one(
            "SELECT * FROM trades WHERE mode=? AND symbol=? AND status IN ('entering','open','exiting') "
            "ORDER BY id DESC LIMIT 1",
            (mode, symbol),
        )

    def _trade(self, trade_id: int) -> dict:
        return self._db().query_one("SELECT * FROM trades WHERE id=?", (trade_id,)) or {}

    def _update(self, trade_id: int, **fields) -> None:
        fields["updated_at"] = iso(self.ctx._now())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._db().execute(f"UPDATE trades SET {cols} WHERE id=?", (*fields.values(), trade_id))

    def _decide(self, mode: str, symbol: str, signal: str, action: str, reason: str, **extra) -> None:
        self._db().add_decision(mode, symbol=symbol, signal=signal, action=action, reason=reason, **extra)

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

    def _from_snapshot(self, mode: str, cid: str | None) -> Order | None:
        snap = self.ctx.runtimes[mode].snapshot
        if not cid or snap is None:
            return None
        return next((o for o in [*snap.open_orders, *snap.recent_orders] if o.client_order_id == cid), None)

    # --------------------------------------------------------------- tick
    async def tick(self, mode: str, params: BotParams) -> None:
        async with self._lock:
            await self._tick(mode, params)

    async def finalize_all(self, mode: str) -> None:
        """Hintergrund (auch bei Pause): Ein-/Ausstiegs- und Schutzorders nur lesend abgleichen."""
        async with self._lock:
            now = self.ctx._now()
            for trade in self._db().query(
                "SELECT * FROM trades WHERE mode=? AND (status IN ('entering','exiting') "
                "OR (status='open' AND stop_cid IS NOT NULL))",
                (mode,),
            ):
                await self.finalize(mode, trade, now)

    async def _tick(self, mode: str, params: BotParams) -> None:
        symbols = params.scalp_symbols
        if set(self.syms) != set(symbols) or any(st.bars.seconds != params.bar_seconds for st in self.syms.values()):
            self.reset(params)
        rt = self.ctx.runtimes[mode]
        now = rt._now()
        is_open, to_close = self._session(mode, now)
        # Aktien/ETFs nur bei offener US-Börse abfragen; Krypto rund um die Uhr.
        active = [s for s in symbols if is_crypto(s) or is_open]
        # Echtzeit-Datenstrom (nur PAPER, nur Krypto, nur wenn verbunden); fehlende Kurse kommen per Abfrage.
        stream = self.ctx.streams.market if mode == "PAPER" else None
        quotes: dict[str, Quote] = {}
        if stream is not None:
            for s in active:
                sq = stream.fresh_quote(s, now, QUOTE_MAX_AGE_S)
                if sq is not None:
                    quotes[s] = sq
        missing = [s for s in active if s not in quotes]
        failed = None
        if missing:
            try:
                quotes.update(await rt.broker.get_quotes(missing))
            except BrokerError as exc:
                failed = f"Kursabruf fehlgeschlagen ({exc.kind}): {exc.message}"
        self.state["feed"] = "stream" if not missing else ("poll" if len(missing) == len(active) else "mixed")
        self.state["market_open"] = is_open
        for symbol in symbols:
            st = self.syms[symbol]
            crypto = is_crypto(symbol)
            if not crypto and not is_open:
                await self._market_closed(mode, symbol, st, now)
                continue
            q = quotes.get(symbol)
            fresh = q is not None and (now - q.at).total_seconds() <= QUOTE_MAX_AGE_S
            if q is not None:
                st.view["quote"] = {
                    "bid": q.bid, "ask": q.ask, "spread_pct": round(q.spread_pct, 4),
                    "at": iso(q.at), "fresh": fresh, "source": q.source,
                    "via": "stream" if symbol not in missing else "poll",
                }
            if fresh:
                assert q is not None
                if stream is not None and symbol not in missing:
                    for at, mid in stream.drain(symbol):  # jedes Kurs-Update fliesst in die Mikrokerzen
                        st.bars.add(at, mid)
                st.bars.add(now, q.mid)
            else:
                st.view["status"] = failed or ("Kurs veraltet – keine Einstiege" if q else "Kein Kurs")
            trade = self.active_trade(mode, symbol)
            if trade is not None:
                await self._manage(mode, params, symbol, st, trade, q if fresh else None, now,
                                   None if crypto else to_close)
            else:
                st.view.pop("trade", None)
                block = None
                if not crypto and to_close is not None and to_close <= NO_ENTRY_BEFORE_CLOSE_MIN:
                    block = f"Kurz vor Börsenschluss ({to_close:.0f} Min.) – keine neuen Einstiege"
                elif crypto and is_open and params.crypto_outside_session_only and params.has_stocks:
                    block = "US-Börse offen – Krypto pausiert, Aktien/ETFs handeln"
                await self._maybe_enter(mode, params, symbol, st, q if fresh else None, now, block)
        self.state["symbols"] = {s: self.syms[s].view for s in symbols}
        open_trades = sum(1 for s in symbols if "trade" in self.syms[s].view)
        warm = [s for s in symbols if self.syms[s].view.get("bars_ready", 0) < self.syms[s].view.get("bars_needed", 1)]
        self.state["status"] = failed or (
            f"{len(symbols)} Symbol(e) · {open_trades} Position(en) offen"
            + (f" · Aufwärmen: {', '.join(warm)}" if warm else "")
        )

    def _session(self, mode: str, now: datetime) -> tuple[bool | None, float | None]:
        """US-Börse offen? und Minuten bis Börsenschluss (aus dem letzten Kontoabgleich)."""
        snap = self.ctx.runtimes[mode].snapshot
        if snap is None:
            return None, None
        clock = snap.clock
        if not clock.is_open or clock.next_close is None:
            return clock.is_open, None
        return True, (clock.next_close - now).total_seconds() / 60

    async def _market_closed(self, mode: str, symbol: str, st: _SymState, now: datetime) -> None:
        """Aktie/ETF bei geschlossener Börse: keine Kurse, keine Orders – nur Ein-/Ausstiege abgleichen."""
        if "quote" in st.view:
            st.view["quote"]["fresh"] = False
        trade = self.active_trade(mode, symbol)
        if trade is not None and trade["status"] in ("entering", "exiting"):
            await self.finalize(mode, trade, now)
            trade = self.active_trade(mode, symbol)
        snap = self.ctx.runtimes[mode].snapshot
        nxt = snap.clock.next_open if snap is not None else None
        when = f" – Eröffnung {iso(nxt)}" if nxt else ""
        if trade is not None and trade["status"] == "open":
            st.view["status"] = f"US-Börse geschlossen – Position wird nach Eröffnung beendet{when}"
        else:
            st.view.pop("trade", None)
            st.view["status"] = f"US-Börse geschlossen{when}"

    async def _trend(self, mode: str, params: BotParams, symbol: str, price: float, now: datetime) -> tuple[bool, str]:
        """Trendfilter auf 1-Minuten-Kerzen (zwischengespeichert); Aktien nur Kerzen der laufenden Sitzung."""
        rt = self.ctx.runtimes[mode]
        n = params.trend_minutes
        try:
            bars, _, _ = await self.ctx.market.fetch(rt, symbol, n + TREND_SLOPE_BARS + 30, "1Min")
        except BrokerError as exc:
            return False, f"Minutenkerzen nicht verfügbar ({exc.message})"
        done = [b for b in bars if b.t + timedelta(minutes=1) <= now]
        if not is_crypto(symbol) and rt.broker.uses_sessions:
            try:
                start = await rt.broker.get_session_open(now)
            except BrokerError:
                start = None
            if start is not None:
                done = [b for b in done if b.t >= start]
        return trend_ok([b.c for b in done], n, price)

    # ------------------------------------------------------------ Einstieg
    async def _maybe_enter(self, mode: str, params: BotParams, symbol: str, st: _SymState, quote: Quote | None,
                           now: datetime, block: str | None = None) -> None:
        bars = st.bars.bars()
        needed = params.long_window + 1
        st.view["bars_ready"] = min(len(bars), needed)
        st.view["bars_needed"] = needed
        if not bars:
            return
        latest = bars[-1]
        if st.last_processed is not None and latest.t <= st.last_processed:
            return
        st.last_processed = latest.t  # jede Mikrokerze höchstens einmal
        if len(bars) < needed:
            st.view["status"] = f"Aufwärmen: {len(bars)}/{needed} Mikrokerzen à {params.bar_seconds} s"
            return
        sig = evaluate(bars, params.short_window, params.long_window)
        st.view["last_eval"] = {
            "bar_time": iso(latest.t), "close": latest.c,
            "sma_short": sig.sma_short, "sma_long": sig.sma_long, "signal": sig.kind,
        }
        if sig.kind != "buy":
            st.view["status"] = block or f"Beobachte – {sig.reason}"
            return
        extra = dict(bar_time=iso(latest.t), close=latest.c,
                     sma_short=round(sig.sma_short or 0, 4), sma_long=round(sig.sma_long or 0, 4))
        if block:
            self._decide(mode, symbol, "buy", "skip", f"{sig.reason}; {block}", **extra)
            st.view["status"] = block
            return
        if quote is None:
            self._decide(mode, symbol, "buy", "skip", f"{sig.reason}; kein frischer Kurs – kein Einstieg", **extra)
            return
        crypto = is_crypto(symbol)
        if not crypto:
            # Aktien/ETFs: Spread-Grenze hier prüfen (der Orderweg schätzt Aktien über den letzten Trade).
            limit = get_risk_limits(self._db(), mode).max_spread_pct
            if quote.spread_pct > limit + 1e-12:
                why = f"Spread {quote.spread_pct:.3f} % über der Grenze von {limit:g} %"
                self._decide(mode, symbol, "buy", "blocked", f"{sig.reason}; Einstieg gesperrt – {why}", **extra)
                st.view["status"] = f"Einstieg gesperrt – {why}"
                return
        if params.trend_filter:
            ok, why = await self._trend(mode, params, symbol, quote.mid, now)
            if not ok:
                self._decide(mode, symbol, "buy", "skip", f"{sig.reason}; Trendfilter: {why}", **extra)
                st.view["status"] = f"Signal ausgelassen – Trendfilter: {why}"
                return
        inp = OrderInput(
            symbol=symbol, side="buy", time_in_force="gtc" if crypto else "day",
            notional=Decimal(str(params.order_value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN),
        )
        try:
            res = await self.ctx.orders.submit(mode, inp, source="bot", idempotency_key=f"scalp:{symbol}:{iso(latest.t)}")
        except OrderBlocked as exc:
            failed = [c for c in exc.preview.get("checks", []) if c["blocking"] and not c["ok"]]
            why = "; ".join(f"{c['name']}: {c['detail']}" for c in failed) or "gesperrt"
            self._decide(mode, symbol, "buy", "blocked", f"{sig.reason}; Einstieg gesperrt – {why}", **extra)
            st.view["status"] = f"Einstieg gesperrt – {why}"
            return
        except OrderFailed as exc:
            self._decide(mode, symbol, "buy", "failed", f"{sig.reason}; Broker lehnte ab: {exc.error.message}", **extra)
            return
        if res.get("duplicate"):
            return
        ts = iso(now)
        self._db().execute(
            "INSERT INTO trades (mode, symbol, status, entry_cid, fee_pct, created_at, updated_at) "
            "VALUES (?, ?, 'entering', ?, ?, ?, ?)",
            (mode, symbol, res["order"]["client_order_id"], params.profile(symbol).fee_pct, ts, ts),
        )
        self._decide(mode, symbol, "buy", "order",
                     f"{sig.reason}; Einstieg {params.order_value:g} USD gesendet (Spread {quote.spread_pct:.3f} %)", **extra)
        st.view["status"] = "Einstieg gesendet – warte auf Ausführung"

    # ------------------------------------------------------------ Trades
    async def _manage(self, mode: str, params: BotParams, symbol: str, st: _SymState, trade: dict, quote: Quote | None,
                      now: datetime, to_close: float | None = None) -> None:
        if trade["status"] in ("entering", "exiting"):
            await self.finalize(mode, trade, now)
            trade = self._trade(trade["id"]) or trade
            if trade["status"] != "open":
                return
        # Schutzorder beim Broker nur für Krypto (Aktien/ETFs: Bruchstücke, Glattstellen vor Börsenschluss).
        if params.broker_stop and is_crypto(symbol) and not trade.get("stop_cid"):
            await self._place_stop(mode, params, trade, now)
            trade = self._trade(trade["id"]) or trade
        await self._check_exit(mode, params, symbol, st, trade, quote, now, to_close)

    async def finalize(self, mode: str, trade: dict, now: datetime) -> None:
        """Nur lesend: Ein-/Ausstiegs- und Schutzorders abgleichen (läuft auch bei pausiertem Bot)."""
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
                self._close_trade(mode, self._trade(trade["id"]), order, now)
            elif order.status in FINAL_ORDER_STATUSES:
                self._update(trade["id"], status="open", exit_cid=None, note=f"Ausstieg {order.status} – erneuter Versuch")
        elif trade["status"] == "open" and trade.get("stop_cid"):
            stop = await self._lookup(mode, trade["stop_cid"])
            await self._handle_stop_state(mode, trade, stop, now)

    async def _handle_stop_state(self, mode: str, trade: dict, stop: Order | None, now: datetime) -> str:
        """Wertet die Schutzorder aus. Rückgabe: active | filled | gone | unknown."""
        if stop is None:
            local = self._local_order(mode, trade.get("stop_cid"))
            if local and local["status"] in LOCAL_FINAL:
                self._update(trade["id"], stop_cid=None)
                return "gone"
            return "unknown"
        if stop.status == "filled" or (stop.filled_qty > 0 and stop.status in FINAL_ORDER_STATUSES):
            self._update(trade["id"], status="exiting", exit_cid=stop.client_order_id, exit_reason="Stop-Loss (Schutzorder)")
            self._close_trade(mode, self._trade(trade["id"]), stop, now)
            return "filled"
        if stop.status in FINAL_ORDER_STATUSES:
            self._update(trade["id"], stop_cid=None)  # storniert/abgelaufen → keine Schutzorder mehr
            return "gone"
        return "active"

    def _open_trade(self, mode: str, trade: dict, order: Order, now: datetime) -> None:
        params = get_bot_params(self._db(), mode)
        prof = params.profile(trade["symbol"])
        entry = order.filled_avg_price or 0.0
        entry_time = order.filled_at or now
        self._update(
            trade["id"],
            status="open",
            entry_time=iso(entry_time),
            entry_qty=order.filled_qty,
            entry_price=entry,
            stop_price=round(entry * (1 - prof.stop_loss_pct / 100), 6),
            target_price=round(entry * (1 + prof.take_profit_pct / 100), 6),
            deadline=iso(entry_time + timedelta(minutes=prof.max_hold_min)),
        )
        self._db().add_event(
            mode, "info", "trade",
            f"Einstieg ausgeführt: {order.filled_qty:g} {trade['symbol']} zu {entry:.2f}. "
            f"Stop-Loss −{prof.stop_loss_pct:g} %, Take-Profit +{prof.take_profit_pct:g} %, "
            f"max. {prof.max_hold_min:g} Min.",
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
            f"Trade {trade['symbol']} geschlossen ({trade.get('exit_reason')}): brutto {gross:+.2f}, "
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

    # ------------------------------------------------------ Schutzorder
    async def _place_stop(self, mode: str, params: BotParams, trade: dict, now: datetime) -> None:
        retry = self._stop_retry_after.get(trade["id"])
        if retry is not None and now < retry:
            return
        rt = self.ctx.runtimes[mode]
        if not await rt.sync() or rt.snapshot is None:
            self._stop_retry_after[trade["id"]] = now + timedelta(seconds=STOP_RETRY_S)
            return
        available = available_to_sell(rt.snapshot.positions, rt.snapshot.open_orders, trade["symbol"])
        if available <= 0:
            self._stop_retry_after[trade["id"]] = now + timedelta(seconds=STOP_RETRY_S)
            return  # Position noch nicht sichtbar – später erneut
        try:
            asset = await rt.broker.get_asset(trade["symbol"])
            increment = asset.price_increment
        except BrokerError:
            increment = None
        stop = round_down_price(trade["stop_price"], increment)
        limit = round_down_price(float(stop) * (1 - params.stop_limit_offset_pct / 100), increment)
        qty = _q9(min(trade["entry_qty"] or available, available))
        attempt = int(trade.get("stop_attempts") or 0)
        inp = OrderInput(symbol=trade["symbol"], side="sell", qty=qty, time_in_force="gtc",
                         type="stop_limit", stop_price=stop, limit_price=limit)
        try:
            res = await self.ctx.orders.submit(mode, inp, source="bot", idempotency_key=f"stop:{trade['id']}:{attempt}")
        except OrderBlocked as exc:
            failed = [c for c in exc.preview.get("checks", []) if c["blocking"] and not c["ok"]]
            why = "; ".join(f"{c['name']}: {c['detail']}" for c in failed) or "gesperrt"
            if attempt == 0:
                self._decide(mode, trade["symbol"], "sell", "blocked", f"Schutzorder gesperrt – {why}. Der Bot überwacht den Stop-Loss selbst.")
            self._update(trade["id"], stop_attempts=attempt + 1)
            self._stop_retry_after[trade["id"]] = now + timedelta(seconds=STOP_RETRY_S)
            return
        except OrderFailed as exc:
            self._decide(mode, trade["symbol"], "sell", "failed", f"Schutzorder abgelehnt: {exc.error.message}. Der Bot überwacht den Stop-Loss selbst.")
            self._update(trade["id"], stop_attempts=attempt + 1)
            self._stop_retry_after[trade["id"]] = now + timedelta(seconds=STOP_RETRY_S * 4)
            return
        self._update(trade["id"], stop_cid=res["order"]["client_order_id"], stop_attempts=attempt + 1)
        self._decide(mode, trade["symbol"], "sell", "order",
                     f"Schutzorder beim Broker: Stop {stop}, Limit {limit}, Menge {qty} (bleibt auch bei Pause aktiv)")

    async def _cancel_stop(self, mode: str, trade: dict, now: datetime) -> str:
        """Storniert die Schutzorder. Rückgabe: free (Bestand frei) | filled | wait."""
        stop = await self._lookup(mode, trade.get("stop_cid"))
        state = await self._handle_stop_state(mode, trade, stop, now)
        if state == "filled":
            return "filled"
        if state == "gone":
            return "free"
        if state == "unknown" or stop is None:
            return "wait"
        if stop.status != "pending_cancel":
            try:
                await self.ctx.runtimes[mode].broker.cancel_order(stop.id)
            except BrokerError:
                return "wait"
        stop = await self._lookup(mode, trade.get("stop_cid"))
        state = await self._handle_stop_state(mode, self._trade(trade["id"]), stop, now)
        return {"filled": "filled", "gone": "free"}.get(state, "wait")

    # ------------------------------------------------------------ Ausstieg
    async def _check_exit(self, mode: str, params: BotParams, symbol: str, st: _SymState, trade: dict, quote: Quote | None,
                          now: datetime, to_close: float | None = None) -> None:
        entry = trade["entry_price"] or 0.0
        deadline = parse_iso(trade["deadline"])
        info = {
            "id": trade["id"], "qty": trade["entry_qty"], "entry_price": entry,
            "stop_price": trade["stop_price"], "target_price": trade["target_price"],
            "deadline": trade["deadline"], "entry_time": trade["entry_time"],
            "broker_stop": bool(trade.get("stop_cid")),
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
        if reason is None and to_close is not None and to_close <= FLATTEN_BEFORE_CLOSE_MIN:
            reason = "Börsenschluss"

        # Schutzorder bereits ausgeführt? (zuerst aus dem Abgleich, ohne Extra-Anfrage)
        if trade.get("stop_cid"):
            snap_stop = self._from_snapshot(mode, trade["stop_cid"])
            if snap_stop is not None and not is_open_status(snap_stop.status):
                if await self._handle_stop_state(mode, trade, snap_stop, now) == "filled":
                    st.view.pop("trade", None)
                    return
                trade = self._trade(trade["id"])

        # Position extern geschlossen (z. B. manuell oder Not-Aus)?
        snap = self.ctx.runtimes[mode].snapshot
        entry_time = parse_iso(trade["entry_time"])
        if (
            snap is not None
            and entry_time is not None
            and snap.fetched_at > entry_time + timedelta(seconds=5)
            and not any(p.symbol == symbol and p.qty > 0 for p in snap.positions)
        ):
            if trade.get("stop_cid"):
                stop = await self._lookup(mode, trade["stop_cid"])
                if await self._handle_stop_state(mode, trade, stop, now) == "filled":
                    st.view.pop("trade", None)
                    return
            self._update(trade["id"], status="closed", exit_reason="extern geschlossen", exit_time=iso(now),
                         note="Position beim Broker nicht mehr vorhanden – Ergebnis siehe Auftragstabelle")
            self._db().add_event(mode, "info", "trade", f"Trade {symbol}: Position extern geschlossen.")
            st.view.pop("trade", None)
            return

        st.view["trade"] = info
        if reason is None:
            if trade.get("sl_seen_at"):
                self._update(trade["id"], sl_seen_at=None)  # Kurs hat sich über den Stop erholt
            st.view["status"] = "Position offen – überwache Stop-Loss/Take-Profit/Haltedauer"
            return

        if reason == "Stop-Loss" and trade.get("stop_cid"):
            seen = parse_iso(trade.get("sl_seen_at"))
            if seen is None:
                self._update(trade["id"], sl_seen_at=iso(now))
                st.view["status"] = "Stop-Loss erreicht – Schutzorder beim Broker greift"
                return
            if (now - seen).total_seconds() < STOP_GRACE_S:
                stop = await self._lookup(mode, trade["stop_cid"])
                if await self._handle_stop_state(mode, trade, stop, now) == "filled":
                    st.view.pop("trade", None)
                return
            reason = "Stop-Loss (Schutzorder nicht ausgeführt)"

        retry = self._exit_retry_after.get(trade["id"])
        if retry is not None and now < retry:
            return
        if trade.get("stop_cid"):
            outcome = await self._cancel_stop(mode, trade, now)
            if outcome == "filled":
                st.view.pop("trade", None)
                return
            if outcome == "wait":
                st.view["status"] = f"{reason}: Schutzorder wird storniert …"
                self._exit_retry_after[trade["id"]] = now + timedelta(seconds=2)
                return
            trade = self._trade(trade["id"])
        await self._exit(mode, symbol, st, trade, reason, quote, now)

    async def _exit(self, mode: str, symbol: str, st: _SymState, trade: dict, reason: str, quote: Quote | None, now: datetime) -> None:
        rt = self.ctx.runtimes[mode]
        if not await rt.sync() or rt.snapshot is None:
            st.view["status"] = "Ausstieg verschoben – Brokerzustand nicht abrufbar"
            self._exit_retry_after[trade["id"]] = now + timedelta(seconds=EXIT_RETRY_S)
            return
        available = available_to_sell(rt.snapshot.positions, rt.snapshot.open_orders, symbol)
        if available <= 0:
            self._update(trade["id"], status="closed", exit_reason="extern geschlossen", exit_time=iso(now),
                         note="Kein verfügbarer Bestand mehr")
            st.view.pop("trade", None)
            return
        # Gebühren können den Bestand leicht verringern → höchstens den verfügbaren Bestand verkaufen.
        qty = _q9(min(trade["entry_qty"] or available, available))
        inp = OrderInput(symbol=symbol, side="sell", qty=qty, time_in_force="gtc" if is_crypto(symbol) else "day")
        attempt = int(trade["exit_attempts"] or 0)
        bid = f" (Geldkurs {quote.bid:.2f})" if quote else ""
        try:
            res = await self.ctx.orders.submit(mode, inp, source="bot", idempotency_key=f"exit:{trade['id']}:{attempt}")
        except OrderBlocked as exc:
            failed = [c for c in exc.preview.get("checks", []) if c["blocking"] and not c["ok"]]
            why = "; ".join(f"{c['name']}: {c['detail']}" for c in failed) or "gesperrt"
            self._decide(mode, symbol, "sell", "blocked", f"{reason}{bid}; Ausstieg gesperrt – {why}")
            self._exit_retry_after[trade["id"]] = now + timedelta(seconds=EXIT_RETRY_S)
            return
        except OrderFailed as exc:
            self._update(trade["id"], exit_attempts=attempt + 1)
            self._decide(mode, symbol, "sell", "failed", f"{reason}{bid}; Broker lehnte Ausstieg ab: {exc.error.message}")
            self._exit_retry_after[trade["id"]] = now + timedelta(seconds=EXIT_RETRY_S)
            return
        self._update(trade["id"], status="exiting", exit_cid=res["order"]["client_order_id"], exit_reason=reason,
                     exit_attempts=attempt + 1)
        self._decide(mode, symbol, "sell", "order", f"{reason}{bid}; Verkauf {qty} gesendet")
        st.view["status"] = f"Ausstieg ({reason}) gesendet – warte auf Ausführung"

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
