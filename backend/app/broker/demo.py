"""Simulierter Broker für den DEMO-Modus.

* Keine Netzwerkzugriffe, keine Alpaca-Aufrufe.
* Kurse sind reproduzierbar: Sie werden deterministisch aus Symbol und Zeit
  berechnet (überlagerte Sinuswellen), damit gleitende Durchschnitte sich
  regelmässig kreuzen und die Automatik prüfbar ist.
* Market-Orders werden verzögert gefüllt, grössere Stückzahlen in zwei
  Teilausführungen – so lassen sich Status, Teilausführungen und Abgleich testen.
* Der simulierte Markt ist immer geöffnet (klar als DEMO gekennzeichnet).
"""

from __future__ import annotations

import math
import uuid
import zlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal

from ..db import Database, iso, parse_iso
from ..timeutil import trading_day
from .base import (
    Account,
    Asset,
    Bar,
    Broker,
    BrokerError,
    Clock,
    LatestPrice,
    Order,
    OrderRequest,
    Position,
    Quote,
    is_crypto,
)

DEMO_INITIAL_CASH = 10_000.0
DEMO_ASSETS: dict[str, tuple[str, float, bool]] = {
    # Symbol: (Name, Basispreis, fractionable)
    "SPY": ("DEMO SPDR S&P 500 ETF", 520.0, True),
    "QQQ": ("DEMO Invesco QQQ", 450.0, True),
    "IWM": ("DEMO iShares Russell 2000", 210.0, True),
    "AAPL": ("DEMO Apple", 190.0, True),
    "MSFT": ("DEMO Microsoft", 420.0, True),
    "NVDA": ("DEMO NVIDIA", 120.0, True),
    "F": ("DEMO Ford Motor", 11.5, True),
    "XYZW": ("DEMO nicht handelbar", 25.0, False),
    # Krypto (24/7, stärkere Schwankungen)
    "BTC/USD": ("DEMO Bitcoin", 68_000.0, True),
    "ETH/USD": ("DEMO Ether", 3_400.0, True),
}
DEMO_SPREAD_PCT = 0.02  # simulierter Abstand zwischen Geld- und Briefkurs in %
PARTIAL_FILL_DELAY_S = 1.0
FINAL_FILL_DELAY_S = 3.0


def demo_price(symbol: str, ts: float) -> float:
    """Deterministischer Demo-Kurs zum Zeitpunkt ``ts`` (Unix-Sekunden)."""
    base = DEMO_ASSETS.get(symbol, ("", 100.0, True))[1]
    phase = (zlib.crc32(symbol.encode()) % 1000) / 1000 * 2 * math.pi
    m = ts / 60.0
    wave = (
        0.0060 * math.sin(2 * math.pi * m / 55 + phase)
        + 0.0030 * math.sin(2 * math.pi * m / 17 + 2 * phase)
        + 0.0012 * math.sin(2 * math.pi * m / 4.3 + 3 * phase)
    )
    if is_crypto(symbol):
        # Krypto schwankt stärker und schneller (für Scalping-Tests auf Sekundenbasis).
        wave = 1.6 * wave + 0.0020 * math.sin(2 * math.pi * m / 1.3 + 4 * phase)
    return round(base * (1 + wave), 2)


def demo_quote_prices(symbol: str, ts: float) -> tuple[float, float]:
    """(bid, ask) um den Demo-Kurs mit festem Spread."""
    mid = demo_price(symbol, ts)
    half = mid * DEMO_SPREAD_PCT / 200
    return round(mid - half, 2), round(mid + half, 2)


def demo_bar(symbol: str, minute_start: datetime) -> Bar:
    t0 = minute_start.timestamp()
    samples = [demo_price(symbol, t0 + s) for s in (0, 15, 30, 45, 59.999)]
    vol = 500 + zlib.crc32(f"{symbol}{int(t0)}".encode()) % 4000
    return Bar(t=minute_start, o=samples[0], h=max(samples), l=min(samples), c=samples[-1], v=float(vol))


class DemoBroker(Broker):
    mode = "DEMO"
    data_feed = "demo"
    data_source_label = "DEMO – synthetische, reproduzierbare Beispieldaten (keine echten Kurse)"

    def __init__(self, db: Database, now: Callable[[], datetime] | None = None) -> None:
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        if not self.db.query_one("SELECT id FROM demo_account WHERE id=1"):
            self.reset()

    # ------------------------------------------------------------ helpers
    def reset(self) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM demo_account")
            c.execute("DELETE FROM demo_positions")
            c.execute("DELETE FROM demo_orders")
            c.execute(
                "INSERT INTO demo_account (id, cash, initial_cash, last_equity, last_equity_day) VALUES (1, ?, ?, ?, ?)",
                (DEMO_INITIAL_CASH, DEMO_INITIAL_CASH, DEMO_INITIAL_CASH, trading_day(self._now())),
            )

    def _price(self, symbol: str) -> float:
        return demo_price(symbol, self._now().timestamp())

    def _row_to_order(self, r: dict) -> Order:
        return Order(
            id=r["id"],
            client_order_id=r["client_order_id"],
            symbol=r["symbol"],
            side=r["side"],
            type="market",
            time_in_force=r["time_in_force"],
            qty=r["qty"],
            notional=r["notional"],
            filled_qty=r["filled_qty"],
            filled_avg_price=r["filled_avg_price"],
            status=r["status"],
            created_at=parse_iso(r["created_at"]),
            updated_at=parse_iso(r["updated_at"]),
            submitted_at=parse_iso(r["created_at"]),
            filled_at=parse_iso(r["filled_at"]),
            canceled_at=parse_iso(r["canceled_at"]),
        )

    def _apply_fill(self, c, order: dict, fill_qty: float, price: float, now: datetime) -> str:
        """Bucht eine (Teil-)Ausführung. Gibt den neuen Status zurück."""
        acct = c.execute("SELECT cash FROM demo_account WHERE id=1").fetchone()
        pos = c.execute("SELECT qty, avg_entry_price FROM demo_positions WHERE symbol=?", (order["symbol"],)).fetchone()
        value = fill_qty * price
        if order["side"] == "buy":
            if acct["cash"] + 1e-9 < value:
                return "rejected"
            new_cash = acct["cash"] - value
            if pos:
                qty = pos["qty"] + fill_qty
                avg = (pos["qty"] * pos["avg_entry_price"] + value) / qty
                c.execute("UPDATE demo_positions SET qty=?, avg_entry_price=? WHERE symbol=?", (qty, avg, order["symbol"]))
            else:
                c.execute(
                    "INSERT INTO demo_positions (symbol, qty, avg_entry_price) VALUES (?, ?, ?)",
                    (order["symbol"], fill_qty, price),
                )
        else:
            if not pos or pos["qty"] + 1e-9 < fill_qty:
                return "rejected"
            new_cash = acct["cash"] + value
            left = pos["qty"] - fill_qty
            if left <= 1e-9:
                c.execute("DELETE FROM demo_positions WHERE symbol=?", (order["symbol"],))
            else:
                c.execute("UPDATE demo_positions SET qty=? WHERE symbol=?", (left, order["symbol"]))
        c.execute("UPDATE demo_account SET cash=? WHERE id=1", (new_cash,))
        prev_filled = order["filled_qty"] or 0.0
        total = prev_filled + fill_qty
        avg_px = ((order["filled_avg_price"] or 0.0) * prev_filled + value) / total
        target = order["qty"] if order["qty"] is not None else total
        status = "filled" if total + 1e-9 >= target else "partially_filled"
        c.execute(
            "UPDATE demo_orders SET filled_qty=?, filled_avg_price=?, status=?, updated_at=?, filled_at=? WHERE id=?",
            (total, round(avg_px, 4), status, iso(now), iso(now) if status == "filled" else None, order["id"]),
        )
        return status

    def _advance(self) -> None:
        """Simuliert den Börsenfortschritt für offene Demo-Orders."""
        now = self._now()
        with self.db.transaction() as c:
            rows = c.execute(
                "SELECT * FROM demo_orders WHERE status IN ('new', 'accepted', 'partially_filled')"
            ).fetchall()
            for r in rows:
                order = dict(r)
                age = (now - parse_iso(order["created_at"])).total_seconds()
                bid, ask = demo_quote_prices(order["symbol"], now.timestamp())
                price = ask if order["side"] == "buy" else bid  # Käufe zum Brief-, Verkäufe zum Geldkurs
                if order["notional"] is not None and order["qty"] is None:
                    if age < PARTIAL_FILL_DELAY_S:
                        continue
                    qty = float(
                        (Decimal(str(order["notional"])) / Decimal(str(price))).quantize(
                            Decimal("0.000000001"), rounding=ROUND_DOWN
                        )
                    )
                    c.execute("UPDATE demo_orders SET qty=? WHERE id=?", (qty, order["id"]))
                    order["qty"] = qty
                    status = self._apply_fill(c, order, qty, price, now)
                else:
                    remaining = order["qty"] - (order["filled_qty"] or 0.0)
                    if order["status"] != "partially_filled":
                        if age < PARTIAL_FILL_DELAY_S:
                            continue
                        if order["qty"] >= 2 and float(order["qty"]).is_integer():
                            fill = float(math.floor(order["qty"] / 2))
                        else:
                            fill = remaining
                    else:
                        if age < FINAL_FILL_DELAY_S:
                            continue
                        fill = remaining
                    status = self._apply_fill(c, order, fill, price, now)
                if status == "rejected":
                    c.execute(
                        "UPDATE demo_orders SET status='rejected', updated_at=? WHERE id=?",
                        (iso(now), order["id"]),
                    )

    def _equity(self) -> tuple[float, float]:
        acct = self.db.query_one("SELECT * FROM demo_account WHERE id=1")
        positions = self.db.query("SELECT * FROM demo_positions")
        mv = sum(p["qty"] * self._price(p["symbol"]) for p in positions)
        return acct["cash"], acct["cash"] + mv

    # -------------------------------------------------------- interface
    async def get_account(self) -> Account:
        self._advance()
        cash, equity = self._equity()
        acct = self.db.query_one("SELECT * FROM demo_account WHERE id=1")
        today = trading_day(self._now())
        if acct["last_equity_day"] != today:
            # Tageswechsel: Ausgangswert des neuen Tages festhalten.
            self.db.execute(
                "UPDATE demo_account SET last_equity=?, last_equity_day=? WHERE id=1", (equity, today)
            )
            acct["last_equity"] = equity
        return Account(
            status="ACTIVE",
            currency="USD",
            cash=round(cash, 2),
            equity=round(equity, 2),
            last_equity=acct["last_equity"],
            buying_power=round(max(cash, 0) * 2, 2),  # absichtlich > Cash, wie bei Margin-Konten
            non_marginable_buying_power=round(max(cash, 0), 2),
            multiplier="2",
        )

    async def get_clock(self) -> Clock:
        now = self._now()
        return Clock(timestamp=now, is_open=True, next_open=None, next_close=None)

    async def get_asset(self, symbol: str) -> Asset:
        info = DEMO_ASSETS.get(symbol)
        if info is None:
            raise BrokerError(
                "not_found",
                f"DEMO kennt das Symbol {symbol!r} nicht. Verfügbar: {', '.join(sorted(DEMO_ASSETS))}",
                status_code=404,
            )
        name, _, fractionable = info
        tradable = symbol != "XYZW"
        crypto = is_crypto(symbol)
        return Asset(
            symbol=symbol,
            name=name,
            asset_class="crypto" if crypto else "us_equity",
            exchange="CRYPTO" if crypto else "DEMO",
            status="active",
            tradable=tradable,
            fractionable=fractionable,
        )

    async def list_positions(self) -> list[Position]:
        self._advance()
        open_sells = {
            r["symbol"]: r["remaining"]
            for r in self.db.query(
                "SELECT symbol, SUM(COALESCE(qty, 0) - filled_qty) AS remaining FROM demo_orders "
                "WHERE side='sell' AND status IN ('new', 'accepted', 'partially_filled') GROUP BY symbol"
            )
        }
        out = []
        for p in self.db.query("SELECT * FROM demo_positions ORDER BY symbol"):
            price = self._price(p["symbol"])
            mv = p["qty"] * price
            cost = p["qty"] * p["avg_entry_price"]
            out.append(
                Position(
                    symbol=p["symbol"],
                    qty=p["qty"],
                    qty_available=max(p["qty"] - (open_sells.get(p["symbol"]) or 0.0), 0.0),
                    side="long",
                    avg_entry_price=round(p["avg_entry_price"], 4),
                    market_value=round(mv, 2),
                    cost_basis=round(cost, 2),
                    unrealized_pl=round(mv - cost, 2),
                    current_price=price,
                )
            )
        return out

    async def list_orders(
        self, status: str = "open", limit: int = 100, after: datetime | None = None
    ) -> list[Order]:
        self._advance()
        sql = "SELECT * FROM demo_orders"
        cond, params = [], []
        if status == "open":
            cond.append("status IN ('new', 'accepted', 'partially_filled')")
        elif status == "closed":
            cond.append("status NOT IN ('new', 'accepted', 'partially_filled')")
        if after is not None:
            cond.append("created_at > ?")
            params.append(iso(after))
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_order(r) for r in self.db.query(sql, tuple(params))]

    async def get_order(self, order_id: str) -> Order:
        self._advance()
        r = self.db.query_one("SELECT * FROM demo_orders WHERE id=?", (order_id,))
        if not r:
            raise BrokerError("not_found", "Order nicht gefunden", status_code=404)
        return self._row_to_order(r)

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        self._advance()
        r = self.db.query_one("SELECT * FROM demo_orders WHERE client_order_id=?", (client_order_id,))
        return self._row_to_order(r) if r else None

    async def submit_order(self, req: OrderRequest) -> Order:
        if self.db.query_one("SELECT id FROM demo_orders WHERE client_order_id=?", (req.client_order_id,)):
            raise BrokerError("unprocessable", "client_order_id must be unique", status_code=422)
        asset = await self.get_asset(req.symbol)
        if not asset.tradable:
            raise BrokerError("unprocessable", f"asset {req.symbol} is not tradable", status_code=422)
        if req.side == "sell":
            positions = {p.symbol: p for p in await self.list_positions()}
            pos = positions.get(req.symbol)
            if req.qty is None or pos is None or float(req.qty) > pos.qty_available + 1e-9:
                raise BrokerError("forbidden", "insufficient qty available for order", status_code=403)
        now = self._now()
        oid = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO demo_orders (id, client_order_id, symbol, side, qty, notional, time_in_force, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
            (
                oid,
                req.client_order_id,
                req.symbol,
                req.side,
                float(req.qty) if req.qty is not None else None,
                float(req.notional) if req.notional is not None else None,
                req.time_in_force,
                iso(now),
                iso(now),
            ),
        )
        return await self.get_order(oid)

    async def cancel_order(self, order_id: str) -> None:
        now = self._now()
        r = self.db.query_one("SELECT status FROM demo_orders WHERE id=?", (order_id,))
        if not r:
            raise BrokerError("not_found", "Order nicht gefunden", status_code=404)
        if r["status"] not in ("new", "accepted", "partially_filled"):
            raise BrokerError("unprocessable", "order is not cancelable", status_code=422)
        self.db.execute(
            "UPDATE demo_orders SET status='canceled', canceled_at=?, updated_at=? WHERE id=?",
            (iso(now), iso(now), order_id),
        )

    async def get_bars(self, symbol: str, limit: int) -> list[Bar]:
        if symbol not in DEMO_ASSETS:
            raise BrokerError("not_found", f"DEMO kennt das Symbol {symbol!r} nicht.", status_code=404)
        now = self._now()
        current_minute = now.replace(second=0, microsecond=0)
        # Nur abgeschlossene Minuten, wie bei Alpaca.
        return [demo_bar(symbol, current_minute - timedelta(minutes=i)) for i in range(limit, 0, -1)]

    async def get_latest_price(self, symbol: str) -> LatestPrice | None:
        if symbol not in DEMO_ASSETS:
            return None
        return LatestPrice(price=self._price(symbol), at=self._now(), source="DEMO-Kurs")

    async def get_quote(self, symbol: str) -> Quote | None:
        if symbol not in DEMO_ASSETS:
            return None
        now = self._now()
        bid, ask = demo_quote_prices(symbol, now.timestamp())
        return Quote(symbol=symbol, bid=bid, ask=ask, at=now, source="DEMO-Quote (simulierter Spread)")
