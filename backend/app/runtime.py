"""Laufzeitzustand je Betriebsart: Broker-Snapshot, Verbindungsstatus und Abgleich.

Der Abgleich (Reconcile) holt Konto, Positionen, offene und jüngste Orders vom
Broker und gleicht lokal gespeicherte Orders anhand der client_order_id ab.
Solange der Zustand unklar ist (keine Verbindung, kein Abgleich nach einem
Verbindungsabbruch, Orders mit unbekanntem Ergebnis), sind neue Käufe gesperrt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel

from .broker.base import (
    FINAL_ORDER_STATUSES,
    LOCAL_FINAL,
    LOCAL_NOT_RECEIVED,
    LOCAL_UNRESOLVED,
    Account,
    Broker,
    BrokerError,
    Clock,
    Order,
    Position,
)
from .db import Database, iso, parse_iso
from .timeutil import utcnow

log = logging.getLogger("alpaca_bot.runtime")

# Nach dieser Zeit (und mindestens zwei negativen Abfragen) gilt eine Order mit
# unklarem Ergebnis als nicht beim Broker angekommen.
NOT_RECEIVED_AFTER_S = 120
RECENT_ORDERS_WINDOW = timedelta(days=3)


class Snapshot(BaseModel):
    account: Account
    clock: Clock
    positions: list[Position]
    open_orders: list[Order]
    recent_orders: list[Order]
    fetched_at: datetime


def apply_broker_order(db: Database, mode: str, order: Order, now: datetime) -> bool:
    """Überträgt den Brokerzustand einer Order auf die lokale Zeile. True bei Änderung."""
    row = db.query_one(
        "SELECT * FROM orders WHERE mode=? AND client_order_id=?", (mode, order.client_order_id)
    )
    if not row:
        return False
    changed = (
        row["status"] != order.status
        or abs((row["filled_qty"] or 0) - order.filled_qty) > 1e-12
        or row["broker_order_id"] != order.id
    )
    db.execute(
        "UPDATE orders SET broker_order_id=?, status=?, filled_qty=?, filled_avg_price=?, updated_at=?, "
        "last_checked_at=? WHERE id=?",
        (order.id, order.status, order.filled_qty, order.filled_avg_price, iso(now), iso(now), row["id"]),
    )
    if changed:
        filled = f"{order.filled_qty:g}"
        target = f"{order.qty:g}" if order.qty is not None else (f"{order.notional:g} (Betrag)" if order.notional else "?")
        price = f" zu Ø {order.filled_avg_price:.4f}" if order.filled_avg_price else ""
        level = "warning" if order.status in {"rejected", "expired"} else "info"
        db.add_event(
            mode,
            level,
            "order",
            f"Order {order.symbol} {order.side}: Status {order.status}, ausgeführt {filled}/{target}{price}",
            {"client_order_id": order.client_order_id, "broker_order_id": order.id, "status": order.status},
        )
    return changed


class ModeRuntime:
    def __init__(
        self,
        mode: str,
        broker: Broker,
        db: Database,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.mode = mode
        self.broker = broker
        self.db = db
        self._now = now
        self.snapshot: Snapshot | None = None
        self.last_ok_at: datetime | None = None
        self.last_attempt_at: datetime | None = None
        self.last_error: BrokerError | None = None
        self.last_error_at: datetime | None = None
        self.connection = "unknown"  # unknown | ok | disconnected | error
        self.reconciled = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ state
    def unresolved_orders(self) -> list[dict]:
        marks = ",".join("?" for _ in LOCAL_UNRESOLVED)
        return self.db.query(
            f"SELECT * FROM orders WHERE mode=? AND status IN ({marks}) ORDER BY id",
            (self.mode, *LOCAL_UNRESOLVED),
        )

    def state_issues(self) -> list[str]:
        issues = []
        if self.connection != "ok":
            issues.append("Keine aktuelle Verbindung zum Broker")
        if not self.reconciled:
            issues.append("Abgleich mit dem Broker noch nicht erfolgreich")
        unresolved = self.unresolved_orders()
        if unresolved:
            issues.append(f"{len(unresolved)} Order(s) mit unklarem Ergebnis – Abgleich läuft")
        return issues

    @property
    def state_clear(self) -> bool:
        return not self.state_issues()

    def status_dict(self) -> dict:
        return {
            "mode": self.mode,
            "connection": self.connection,
            "reconciled": self.reconciled,
            "state_clear": self.state_clear,
            "issues": self.state_issues(),
            "last_ok_at": iso(self.last_ok_at),
            "last_attempt_at": iso(self.last_attempt_at),
            "last_error": self.last_error.to_dict() if self.last_error else None,
            "last_error_at": iso(self.last_error_at),
        }

    # ------------------------------------------------------------- sync
    def _failure_backoff_s(self) -> float:
        if self.last_error is not None and self.last_error.kind == "rate_limited":
            return max(self.last_error.retry_after_s or 0, 30.0)
        return 5.0

    def _fail(self, exc: BrokerError) -> None:
        was = self.connection
        self.connection = "disconnected" if exc.is_connectivity or exc.kind == "rate_limited" else "error"
        self.reconciled = False
        self.last_error = exc
        self.last_error_at = self._now()
        if was != self.connection or was == "unknown":
            self.db.add_event(
                self.mode,
                "error",
                "connection",
                f"Brokerabruf fehlgeschlagen ({exc.kind}): {exc.message}",
                {"kind": exc.kind, "hint": exc.hint},
            )

    async def sync(self, max_age_s: float = 0.0) -> bool:
        """Holt den Brokerzustand und gleicht lokale Orders ab. True bei Erfolg."""
        async with self._lock:
            now = self._now()
            if (
                max_age_s
                and self.snapshot is not None
                and self.connection == "ok"
                and self.reconciled
                and (now - self.snapshot.fetched_at).total_seconds() < max_age_s
            ):
                return True
            if (
                max_age_s
                and self.connection in ("error", "disconnected")
                and self.last_attempt_at is not None
                and (now - self.last_attempt_at).total_seconds() < self._failure_backoff_s()
            ):
                return False  # Fehler nicht im Sekundentakt wiederholen (API-Limits)
            self.last_attempt_at = now
            was_reconciled = self.reconciled
            try:
                account = await self.broker.get_account()
                clock = await self.broker.get_clock()
                positions = await self.broker.list_positions()
                open_orders = await self.broker.list_orders(status="open", limit=500)
                recent = await self.broker.list_orders(status="all", limit=200, after=now - RECENT_ORDERS_WINDOW)
                for o in {o.id: o for o in [*recent, *open_orders]}.values():
                    apply_broker_order(self.db, self.mode, o, now)
                await self._resolve_stragglers(now, {o.client_order_id for o in [*recent, *open_orders]})
            except BrokerError as exc:
                self._fail(exc)
                return False
            self.snapshot = Snapshot(
                account=account,
                clock=clock,
                positions=positions,
                open_orders=open_orders,
                recent_orders=recent,
                fetched_at=now,
            )
            if self.connection != "ok" or not was_reconciled:
                self.db.add_event(
                    self.mode,
                    "info",
                    "connection",
                    "Abgleich mit dem Broker erfolgreich (Konto, Positionen, offene Orders).",
                )
            self.connection = "ok"
            self.reconciled = True
            self.last_ok_at = now
            self.last_error = None
            return True

    async def _resolve_stragglers(self, now: datetime, seen: set[str]) -> None:
        """Lokale Orders, deren Brokerzustand nicht im Listenabruf enthalten war."""
        marks = ",".join("?" for _ in FINAL_ORDER_STATUSES | LOCAL_FINAL)
        rows = self.db.query(
            f"SELECT * FROM orders WHERE mode=? AND status NOT IN ({marks})",
            (self.mode, *(FINAL_ORDER_STATUSES | LOCAL_FINAL)),
        )
        for row in rows:
            if row["client_order_id"] in seen:
                continue
            order = await self.broker.get_order_by_client_id(row["client_order_id"])
            if order is not None:
                apply_broker_order(self.db, self.mode, order, now)
                continue
            if row["status"] not in LOCAL_UNRESOLVED:
                # Order war bekannt, ist beim Broker aber nicht mehr auffindbar.
                self.db.execute(
                    "UPDATE orders SET last_checked_at=?, check_count=check_count+1 WHERE id=?",
                    (iso(now), row["id"]),
                )
                continue
            checks = row["check_count"] + 1
            age = (now - parse_iso(row["created_at"])).total_seconds()
            if checks >= 2 and age >= NOT_RECEIVED_AFTER_S:
                self.db.execute(
                    "UPDATE orders SET status=?, check_count=?, last_checked_at=?, updated_at=?, message=? WHERE id=?",
                    (
                        LOCAL_NOT_RECEIVED,
                        checks,
                        iso(now),
                        iso(now),
                        "Broker kennt diese client_order_id nicht – Order wurde nicht übermittelt. Keine automatische Wiederholung.",
                        row["id"],
                    ),
                )
                self.db.add_event(
                    self.mode,
                    "warning",
                    "order",
                    f"Order {row['symbol']} {row['side']} ({row['client_order_id']}) ist beim Broker nicht angekommen. "
                    "Sie wird nicht automatisch erneut gesendet.",
                )
            else:
                self.db.execute(
                    "UPDATE orders SET check_count=?, last_checked_at=? WHERE id=?",
                    (checks, iso(now), row["id"]),
                )
