"""Serverseitige Risikoprüfung – gilt für alle Orderwege (manuell und Bot).

Tagesverlust = aktueller Kontowert (inkl. unrealisierter Gewinne/Verluste)
               − Ausgangswert des Handelstags.
Ausgangswert: ``last_equity`` des Brokers (Kontowert zum letzten Handelsschluss);
fehlt dieser, der erste beobachtete Kontowert des Tages. Ohne Kontodaten gibt es
keine ausreichende Ausgangsbasis → Käufe gesperrt.
Eine verletzte Verlustgrenze sperrt neue Käufe bis zum nächsten Handelstag
(00:00 Uhr New York). Risikoreduzierende Verkäufe bleiben möglich.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .broker.base import Account, Order, Position
from .db import Database, iso, parse_iso
from .runtime import Snapshot
from .settings_store import RiskLimits
from .timeutil import trading_day, utcnow

RESET_RULE = "Automatisch zu Beginn des nächsten Handelstags (00:00 Uhr New York)"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def open_sell_remaining(open_orders: list[Order], symbol: str) -> float:
    return sum(
        max((o.qty or 0.0) - o.filled_qty, 0.0)
        for o in open_orders
        if o.symbol == symbol and o.side == "sell"
    )


def available_to_sell(positions: list[Position], open_orders: list[Order], symbol: str) -> float:
    pos = next((p for p in positions if p.symbol == symbol), None)
    if pos is None or pos.side != "long" or pos.qty <= 0:
        return 0.0
    by_orders = pos.qty - open_sell_remaining(open_orders, symbol)
    return max(min(pos.qty_available, by_orders), 0.0)


class RiskEngine:
    def __init__(self, db: Database, now: Callable[[], datetime] = utcnow) -> None:
        self.db = db
        self._now = now

    # ------------------------------------------------------- Tagesverlust
    def daily_status(self, mode: str, account: Account | None, limits: RiskLimits) -> dict:
        now = self._now()
        today = trading_day(now)
        state = self.db.get(mode, "daily_risk")
        if account is None:
            locked = bool(state and state.get("day") == today and state.get("locked"))
            return {
                "day": today,
                "data_ok": False,
                "reason": "Keine aktuellen Kontodaten – Tagesverlust nicht berechenbar, Käufe gesperrt",
                "locked": locked,
                "limit": limits.max_daily_loss,
                "reset_rule": RESET_RULE,
            }
        if not state or state.get("day") != today:
            if state and state.get("locked"):
                self.db.add_event(mode, "info", "risk", "Tageswechsel: Verlustsperre des Vortags aufgehoben.")
            if account.last_equity and account.last_equity > 0:
                baseline = account.last_equity
                source = "Broker-Kontowert zum letzten Handelsschluss (last_equity)"
            else:
                baseline = account.equity
                source = "Erster beobachteter Kontowert des Tages (kein last_equity verfügbar)"
            state = {"day": today, "baseline": baseline, "source": source, "locked": False, "locked_at": None}
        pnl = account.equity - state["baseline"]
        if not state["locked"] and pnl <= -limits.max_daily_loss:
            state["locked"] = True
            state["locked_at"] = iso(now)
            self.db.add_event(
                mode,
                "warning",
                "risk",
                f"Tagesverlustgrenze erreicht ({pnl:.2f} ≤ −{limits.max_daily_loss:.2f}). "
                "Neue Käufe gesperrt bis zum nächsten Handelstag; Verkäufe bleiben möglich.",
            )
        self.db.set(mode, "daily_risk", state)
        return {
            "day": today,
            "data_ok": True,
            "baseline": round(state["baseline"], 2),
            "baseline_source": state["source"],
            "equity": round(account.equity, 2),
            "pnl": round(pnl, 2),
            "limit": limits.max_daily_loss,
            "locked": state["locked"],
            "locked_at": state["locked_at"],
            "reset_rule": RESET_RULE,
        }

    def buys_since(self, mode: str, since: datetime) -> int:
        """Kauforders seit ``since`` (ohne abgelehnte bzw. nie angekommene)."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM orders WHERE mode=? AND side='buy' AND created_at>=? "
            "AND status NOT IN ('local_failed', 'local_not_received')",
            (mode, iso(since)),
        )
        return int(row["n"]) if row else 0

    def buys_today(self, mode: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM orders WHERE mode=? AND side='buy' AND trading_day=? "
            "AND status NOT IN ('local_failed', 'local_not_received')",
            (mode, trading_day(self._now())),
        )
        return int(row["n"]) if row else 0

    def loss_pause(self, mode: str) -> dict | None:
        """Aktive Kaufpause nach Verlustserie (gesetzt von der Scalping-Engine) oder None."""
        state = self.db.get(mode, "loss_pause")
        if not state:
            return None
        until = parse_iso(state.get("until"))
        if until is None or until <= self._now():
            return None
        return state

    def orders_today(self, mode: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM orders WHERE mode=? AND trading_day=?",
            (mode, trading_day(self._now())),
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------ Prüfung
    def _reserved_for_open_buys(self, mode: str, snap: Snapshot, limits: RiskLimits) -> float:
        prices = {p.symbol: p.current_price for p in snap.positions if p.current_price}
        reserved = 0.0
        for o in snap.open_orders:
            if o.side != "buy":
                continue
            if o.notional is not None:
                reserved += max(o.notional - o.filled_qty * (o.filled_avg_price or 0.0), 0.0)
                continue
            remaining = max((o.qty or 0.0) - o.filled_qty, 0.0)
            price = prices.get(o.symbol)
            if price is None:
                row = self.db.query_one(
                    "SELECT est_price FROM orders WHERE mode=? AND client_order_id=?", (mode, o.client_order_id)
                )
                price = row["est_price"] if row and row["est_price"] else None
            # Unbekannter Preis (z. B. extern angelegte Order): konservativ mit dem Orderlimit ansetzen.
            reserved += remaining * price if price else limits.max_order_value
        return reserved

    def evaluate(
        self,
        mode: str,
        snap: Snapshot,
        limits: RiskLimits,
        *,
        side: str,
        symbol: str,
        est_value: float | None,
        qty: float | None,
        spread_pct: float | None = None,
    ) -> list[Check]:
        checks: list[Check] = []
        orders_today = self.orders_today(mode)
        if side == "buy":
            daily = self.daily_status(mode, snap.account, limits)
            if not daily["data_ok"]:
                checks.append(Check("Tagesverlust", False, daily["reason"]))
            else:
                checks.append(
                    Check(
                        "Tagesverlust",
                        not daily["locked"],
                        f"Heute {daily['pnl']:+.2f} (Grenze −{limits.max_daily_loss:.2f})"
                        + (" – Sperre aktiv bis nächster Handelstag" if daily["locked"] else ""),
                    )
                )
            if est_value is None:
                checks.append(Check("Orderwert", False, "Kein aktueller Kurs – Orderwert nicht bestimmbar"))
                return checks
            buffered = est_value * (1 + limits.price_buffer_pct / 100)
            checks.append(
                Check(
                    "Orderwert",
                    est_value <= limits.max_order_value + 1e-9,
                    f"{est_value:.2f} (max. {limits.max_order_value:.2f})",
                )
            )
            invested = sum(max(p.market_value, 0.0) for p in snap.positions)
            reserved = self._reserved_for_open_buys(mode, snap, limits)
            total = invested + reserved + est_value
            checks.append(
                Check(
                    "Investierter Gesamtbetrag",
                    total <= limits.max_total_invested + 1e-9,
                    f"Positionen {invested:.2f} + offene Käufe {reserved:.2f} + Order {est_value:.2f} = "
                    f"{total:.2f} (max. {limits.max_total_invested:.2f})",
                )
            )
            held = {p.symbol for p in snap.positions if p.qty > 0}
            pending = {o.symbol for o in snap.open_orders if o.side == "buy"}
            symbols = held | pending
            count_after = len(symbols | {symbol})
            checks.append(
                Check(
                    "Offene Positionen",
                    count_after <= limits.max_open_positions,
                    f"{count_after} nach dieser Order (max. {limits.max_open_positions})",
                )
            )
            checks.append(
                Check(
                    "Orders pro Tag",
                    orders_today + 1 <= limits.max_orders_per_day,
                    f"{orders_today} heute (max. {limits.max_orders_per_day})",
                )
            )
            last_hour = self.buys_since(mode, self._now() - timedelta(hours=1))
            checks.append(
                Check(
                    "Einstiege pro Stunde",
                    last_hour + 1 <= limits.max_buys_per_hour,
                    f"{last_hour} in der letzten Stunde (max. {limits.max_buys_per_hour})",
                )
            )
            today = self.buys_today(mode)
            checks.append(
                Check(
                    "Einstiege pro Tag",
                    today + 1 <= limits.max_buys_per_day,
                    f"{today} heute (max. {limits.max_buys_per_day})",
                )
            )
            pause = self.loss_pause(mode)
            checks.append(
                Check(
                    "Pause nach Verlustserie",
                    pause is None,
                    "keine Pause aktiv"
                    if pause is None
                    else f"Käufe pausiert bis {pause['until']} ({pause.get('reason', 'Verlustserie')})",
                )
            )
            if spread_pct is not None:
                checks.append(
                    Check(
                        "Spread",
                        spread_pct <= limits.max_spread_pct + 1e-12,
                        f"{spread_pct:.3f} % (max. {limits.max_spread_pct:g} %)",
                    )
                )
            acct = snap.account
            own = acct.cash
            if acct.non_marginable_buying_power is not None:
                own = min(own, acct.non_marginable_buying_power)
            own = max(own, 0.0) - reserved
            checks.append(
                Check(
                    "Eigene Mittel (ohne Kredit)",
                    buffered <= own + 1e-9,
                    f"Bedarf inkl. {limits.price_buffer_pct:g} % Puffer {buffered:.2f}, verfügbar {max(own, 0):.2f} "
                    "(Cash abzüglich offener Käufe; Buying Power wird nicht als Eigenkapital gewertet)",
                )
            )
        else:
            available = available_to_sell(snap.positions, snap.open_orders, symbol)
            pos = next((p for p in snap.positions if p.symbol == symbol), None)
            if pos is None or pos.qty <= 0:
                checks.append(Check("Bestand", False, "Keine Long-Position vorhanden – Leerverkäufe sind nicht erlaubt"))
            elif pos.side != "long":
                checks.append(Check("Bestand", False, "Nur Long-Positionen werden unterstützt"))
            else:
                want = qty or 0.0
                checks.append(
                    Check(
                        "Verfügbarer Bestand",
                        0 < want <= available + 1e-9,
                        f"Verkauf {want:g}, verfügbar {available:g} (Bestand {pos.qty:g}, durch offene Verkäufe gebunden "
                        f"{pos.qty - available:g})",
                    )
                )
            checks.append(
                Check(
                    "Orders pro Tag",
                    True,
                    f"{orders_today} heute (max. {limits.max_orders_per_day}) – risikoreduzierende Verkäufe bleiben erlaubt",
                    blocking=False,
                )
            )
        return checks
