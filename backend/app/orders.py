"""Gemeinsamer Orderweg für manuelle und automatische Aufträge.

Ablauf jeder Order:
1. Frischer Abgleich mit dem Broker (Konto, Positionen, offene Orders).
2. Prüfung von Freigabe, Kontostatus, Instrument, Menge/Betrag, Gültigkeit.
3. Gemeinsame Risikoprüfung (risk.py).
4. client_order_id lokal speichern – erst danach senden.
5. Senden ohne automatische Wiederholung. Bei unklarem Ergebnis (Timeout,
   Verbindungsabbruch nach dem Senden, 5xx) wird zuerst der Brokerzustand über
   die client_order_id abgefragt; bis zur Klärung sind neue Käufe gesperrt.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from .broker.base import (
    LOCAL_FAILED,
    LOCAL_PENDING,
    LOCAL_UNKNOWN,
    SYMBOL_RE,
    BrokerError,
    is_crypto,
    OrderRequest,
    is_open_status,
)
from .db import iso
from .risk import Check, available_to_sell
from .runtime import apply_broker_order
from .settings_store import get_risk_limits
from .timeutil import trading_day

if TYPE_CHECKING:  # pragma: no cover
    from .core import AppContext

ALLOWED_ASSET_CLASSES = {"us_equity", "crypto"}
EXCLUDED_EQUITY_EXCHANGES = {"OTC", "CRYPTO"}
MAX_QTY_DECIMALS = 9
MAX_NOTIONAL_DECIMALS = 2
MIN_NOTIONAL = Decimal("1")


class OrderInput(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    side: Literal["buy", "sell"]
    qty: Decimal | None = None
    notional: Decimal | None = None
    time_in_force: Literal["day", "gtc"] = "day"


class OrderBlocked(Exception):
    def __init__(self, preview: dict) -> None:
        super().__init__("Order durch Prüfungen gesperrt")
        self.preview = preview


class OrderFailed(Exception):
    def __init__(self, error: BrokerError, order: dict) -> None:
        super().__init__(error.message)
        self.error = error
        self.order = order


def _decimals(value: Decimal) -> int:
    exp = value.normalize().as_tuple().exponent
    return max(-exp, 0) if isinstance(exp, int) else 0


def new_client_order_id(mode: str, source: str) -> str:
    return f"apb-{mode[0].lower()}{source[0].lower()}-{uuid.uuid4().hex}"


class OrderService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------ prüfen
    async def _prepare(self, mode: str, inp: OrderInput, source: str) -> dict:
        ctx = self.ctx
        rt = ctx.runtimes[mode]
        limits = get_risk_limits(ctx.db, mode)
        symbol = inp.symbol.strip().upper()
        checks: list[Check] = []
        warnings: list[str] = []
        preview: dict = {
            "mode": mode,
            "source": source,
            "symbol": symbol,
            "side": inp.side,
            "type": "market",
            "qty": str(inp.qty) if inp.qty is not None else None,
            "notional": str(inp.notional) if inp.notional is not None else None,
            "time_in_force": inp.time_in_force,
            "currency": None,
            "est_price": None,
            "est_price_source": None,
            "est_price_at": None,
            "est_value": None,
            "asset_name": None,
            "market_open": None,
            "checks": [],
            "warnings": warnings,
            "allowed": False,
        }

        def done() -> dict:
            preview["checks"] = [c.to_dict() for c in checks]
            preview["allowed"] = all(c.ok for c in checks if c.blocking)
            return preview

        if mode == "PAPER":
            enabled = ctx.orders_enabled(mode)
            checks.append(
                Check(
                    "Paper-Orderfreigabe",
                    enabled,
                    "Freigabe aktiv" if enabled else "Freigabe ist aus – Paper-Orders sind gesperrt",
                )
            )
        if not SYMBOL_RE.match(symbol):
            checks.append(Check("Symbol", False, "Ungültiges Symbol"))
            return done()

        # 1. Frischer Brokerzustand
        if not await rt.sync():
            err = rt.last_error
            checks.append(Check("Brokerzustand", False, f"Abruf fehlgeschlagen: {err.message if err else 'unbekannt'}"))
            return done()
        snap = rt.snapshot
        assert snap is not None
        preview["currency"] = snap.account.currency
        preview["market_open"] = snap.clock.is_open
        if inp.side == "buy":
            issues = rt.state_issues()
            checks.append(Check("Zustand abgeglichen", not issues, "; ".join(issues) or "Brokerzustand ist abgeglichen"))

        # 2. Konto
        acct = snap.account
        acct_ok = acct.status == "ACTIVE" and not (
            acct.trading_blocked or acct.account_blocked or acct.trade_suspended_by_user
        )
        checks.append(
            Check(
                "Kontostatus",
                acct_ok,
                f"Status {acct.status}"
                + (", Handel gesperrt" if acct.trading_blocked else "")
                + (", Konto gesperrt" if acct.account_blocked else "")
                + (", Handel vom Nutzer pausiert" if acct.trade_suspended_by_user else ""),
            )
        )

        # 3. Instrument
        try:
            asset = await rt.broker.get_asset(symbol)
        except BrokerError as exc:
            checks.append(Check("Instrument", False, exc.message))
            return done()
        preview["asset_name"] = asset.name
        asset_problems = []
        crypto = asset.asset_class == "crypto"
        preview["asset_class"] = asset.asset_class
        if asset.asset_class not in ALLOWED_ASSET_CLASSES:
            asset_problems.append(f"Klasse {asset.asset_class} (nur US-Aktien/ETFs und Krypto gegen USD)")
        if crypto != is_crypto(symbol):
            asset_problems.append("Symbol passt nicht zur Instrumentklasse")
        if asset.status != "active":
            asset_problems.append(f"Status {asset.status}")
        if not asset.tradable:
            asset_problems.append("laut Broker nicht handelbar")
        if not crypto and asset.exchange.upper() in EXCLUDED_EQUITY_EXCHANGES:
            asset_problems.append(f"Handelsplatz {asset.exchange} nicht unterstützt")
        checks.append(
            Check(
                "Instrument",
                not asset_problems,
                "; ".join(asset_problems) or f"{asset.name or symbol} ({asset.exchange}), handelbar"
                + (", Bruchstücke möglich" if asset.fractionable else ", nur ganze Stück"),
            )
        )

        # 4. Menge/Betrag und Gültigkeit (Alpaca-Regeln)
        qty, notional = inp.qty, inp.notional
        if (qty is None) == (notional is None):
            checks.append(Check("Menge/Betrag", False, "Genau eines angeben: Stückzahl oder Geldbetrag"))
            return done()
        if qty is not None:
            fractional = qty != qty.to_integral_value()
            problems = []
            if qty <= 0:
                problems.append("Stückzahl muss grösser als 0 sein")
            if _decimals(qty) > MAX_QTY_DECIMALS:
                problems.append(f"höchstens {MAX_QTY_DECIMALS} Nachkommastellen")
            if fractional and not asset.fractionable:
                problems.append("Bruchstücke sind für dieses Instrument nicht erlaubt")
            if crypto:
                if inp.time_in_force != "gtc":
                    problems.append("Krypto-Orders nur mit Gültigkeit 'gtc'")
                if asset.min_order_size and qty < Decimal(str(asset.min_order_size)):
                    problems.append(f"Mindestmenge {asset.min_order_size:g}")
            elif fractional and inp.time_in_force != "day":
                problems.append("Bruchstück-Orders nur mit Gültigkeit 'day'")
            checks.append(Check("Menge/Gültigkeit", not problems, "; ".join(problems) or f"{qty} Stück, {inp.time_in_force}"))
        else:
            assert notional is not None
            problems = []
            if inp.side != "buy":
                problems.append("Geldbetrag nur für Käufe – Verkäufe bitte als Stückzahl")
            if notional < MIN_NOTIONAL:
                problems.append(f"Mindestbetrag {MIN_NOTIONAL}")
            if _decimals(notional) > MAX_NOTIONAL_DECIMALS:
                problems.append(f"höchstens {MAX_NOTIONAL_DECIMALS} Nachkommastellen")
            if not asset.fractionable:
                problems.append("Betragsorders nur für Instrumente mit Bruchstückhandel")
            required_tif = "gtc" if crypto else "day"
            if inp.time_in_force != required_tif:
                problems.append(f"Betragsorders {'für Krypto ' if crypto else ''}nur mit Gültigkeit '{required_tif}'")
            checks.append(
                Check("Betrag/Gültigkeit", not problems, "; ".join(problems) or f"{notional} {acct.currency}, {inp.time_in_force}")
            )

        # 5. Kurs für Schätzung und Risikoprüfung
        price = None
        spread_pct = None
        lp = None
        if crypto:
            # Krypto: bester Geld-/Briefkurs – Käufe zahlen den Brief-, Verkäufe erhalten den Geldkurs.
            try:
                q = await rt.broker.get_quote(symbol)
            except BrokerError as exc:
                q = None
                warnings.append(f"Aktueller Kurs nicht abrufbar: {exc.message}")
            if q is not None:
                price = q.ask if inp.side == "buy" else q.bid
                spread_pct = q.spread_pct
                preview["est_price_source"] = f"{q.source}, {'Brief' if inp.side == 'buy' else 'Geld'}kurs"
                preview["est_price_at"] = iso(q.at)
                preview["spread_pct"] = round(spread_pct, 4)
        else:
            try:
                lp = await rt.broker.get_latest_price(symbol)
            except BrokerError as exc:
                warnings.append(f"Aktueller Kurs nicht abrufbar: {exc.message}")
        if lp is not None:
            price = lp.price
            preview["est_price_source"] = lp.source
            preview["est_price_at"] = iso(lp.at)
        elif price is None:
            pos = next((p for p in snap.positions if p.symbol == symbol), None)
            if pos and pos.current_price:
                price = pos.current_price
                preview["est_price_source"] = "Positionskurs des Brokers"
        preview["est_price"] = price
        est_value = float(notional) if notional is not None else (float(qty) * price if price else None)
        preview["est_value"] = round(est_value, 2) if est_value is not None else None

        if crypto:
            warnings.append("Krypto wird rund um die Uhr gehandelt; Alpaca berechnet je Ausführung eine Gebühr.")
        elif not snap.clock.is_open:
            warnings.append(
                "Markt ist geschlossen – eine Market-Order mit Gültigkeit 'day' wird bis zur nächsten Eröffnung vorgemerkt."
            )
        warnings.append("Eine angenommene Order ist noch kein ausgeführter Trade; der Status wird laufend abgeglichen.")

        # 6. Gemeinsame Risikoprüfung
        checks.extend(
            ctx.risk.evaluate(
                mode,
                snap,
                limits,
                side=inp.side,
                symbol=symbol,
                est_value=est_value,
                qty=float(qty) if qty is not None else None,
                spread_pct=spread_pct,
            )
        )
        return done()

    async def preview(self, mode: str, inp: OrderInput, source: str = "manual") -> dict:
        return await self._prepare(mode, inp, source)

    # ----------------------------------------------------------- senden
    def _row(self, row_id: int) -> dict:
        return self.ctx.db.query_one("SELECT * FROM orders WHERE id=?", (row_id,)) or {}

    async def submit(
        self, mode: str, inp: OrderInput, *, source: str, idempotency_key: str
    ) -> dict:
        db = self.ctx.db
        async with self.lock:
            existing = db.query_one(
                "SELECT * FROM orders WHERE mode=? AND idempotency_key=?", (mode, idempotency_key)
            )
            if existing:
                return {"duplicate": True, "order": existing, "preview": None}
            preview = await self._prepare(mode, inp, source)
            if not preview["allowed"]:
                raise OrderBlocked(preview)

            rt = self.ctx.runtimes[mode]
            now = rt._now()
            cid = new_client_order_id(mode, source)
            cur = db.execute(
                "INSERT INTO orders (mode, client_order_id, idempotency_key, source, symbol, side, qty, notional, "
                "time_in_force, status, est_price, est_value, trading_day, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mode,
                    cid,
                    idempotency_key,
                    source,
                    preview["symbol"],
                    inp.side,
                    preview["qty"],
                    preview["notional"],
                    inp.time_in_force,
                    LOCAL_PENDING,
                    preview["est_price"],
                    preview["est_value"],
                    trading_day(now),
                    iso(now),
                    iso(now),
                ),
            )
            row_id = int(cur.lastrowid)
            req = OrderRequest(
                symbol=preview["symbol"],
                side=inp.side,
                qty=inp.qty,
                notional=inp.notional,
                time_in_force=inp.time_in_force,
                client_order_id=cid,
            )
            try:
                order = await rt.broker.submit_order(req)
            except BrokerError as exc:
                if exc.may_have_been_sent:
                    db.execute(
                        "UPDATE orders SET status=?, error_kind=?, message=?, updated_at=? WHERE id=?",
                        (LOCAL_UNKNOWN, exc.kind, exc.message, iso(now), row_id),
                    )
                    db.add_event(
                        mode,
                        "warning",
                        "order",
                        f"Ergebnis der Order {preview['symbol']} {inp.side} unklar ({exc.kind}). "
                        "Es wird zuerst der Brokerzustand abgefragt; keine automatische Wiederholung. "
                        "Neue Käufe sind bis zur Klärung gesperrt.",
                        {"client_order_id": cid},
                    )
                    try:
                        found = await rt.broker.get_order_by_client_id(cid)
                    except BrokerError:
                        found = None
                    if found is not None:
                        apply_broker_order(db, mode, found, rt._now())
                    return {"duplicate": False, "order": self._row(row_id), "preview": preview, "uncertain": found is None}
                db.execute(
                    "UPDATE orders SET status=?, error_kind=?, message=?, updated_at=? WHERE id=?",
                    (LOCAL_FAILED, exc.kind, exc.message, iso(now), row_id),
                )
                db.add_event(
                    mode, "error", "order", f"Order {preview['symbol']} {inp.side} abgelehnt: {exc.message}", {"client_order_id": cid}
                )
                raise OrderFailed(exc, self._row(row_id)) from exc

            apply_broker_order(db, mode, order, rt._now())
            await rt.sync()  # Snapshot sofort aktualisieren (neue offene Order sichtbar)
            db.add_event(
                mode,
                "info",
                "order",
                f"Order {preview['symbol']} {inp.side} übermittelt ({'Bot' if source == 'bot' else 'manuell'}), "
                f"Brokerstatus {order.status}. Angenommen bedeutet noch nicht ausgeführt.",
                {"client_order_id": cid, "broker_order_id": order.id},
            )
            return {"duplicate": False, "order": self._row(row_id), "preview": preview}

    # --------------------------------------------------- weitere Aktionen
    async def cancel(self, mode: str, broker_order_id: str) -> None:
        rt = self.ctx.runtimes[mode]
        async with self.lock:
            await rt.broker.cancel_order(broker_order_id)
            self.ctx.db.add_event(mode, "info", "order", f"Stornierung angefordert für Order {broker_order_id}.")
            await rt.sync()

    async def cancel_all_open(self, mode: str) -> dict:
        rt = self.ctx.runtimes[mode]
        async with self.lock:
            if not await rt.sync():
                raise rt.last_error or BrokerError("broker_error", "Brokerzustand nicht abrufbar")
            assert rt.snapshot is not None
            results = []
            for o in rt.snapshot.open_orders:
                if not is_open_status(o.status):
                    continue
                try:
                    await rt.broker.cancel_order(o.id)
                    results.append({"id": o.id, "symbol": o.symbol, "ok": True})
                except BrokerError as exc:
                    results.append({"id": o.id, "symbol": o.symbol, "ok": False, "error": exc.message})
            self.ctx.db.add_event(mode, "info", "order", f"Stornierung aller offenen Orders angefordert ({len(results)}).")
            await rt.sync()
            return {"results": results}

    async def close_position(self, mode: str, symbol: str, idempotency_key: str) -> dict:
        rt = self.ctx.runtimes[mode]
        if not await rt.sync():
            raise rt.last_error or BrokerError("broker_error", "Brokerzustand nicht abrufbar")
        assert rt.snapshot is not None
        qty = available_to_sell(rt.snapshot.positions, rt.snapshot.open_orders, symbol)
        if qty <= 0:
            raise OrderBlocked(
                {
                    "allowed": False,
                    "checks": [Check("Verfügbarer Bestand", False, "Kein verfügbarer Bestand zum Schliessen").to_dict()],
                    "warnings": [],
                }
            )
        q = Decimal(str(qty)).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN).normalize()
        inp = OrderInput(symbol=symbol, side="sell", qty=q, time_in_force="gtc" if is_crypto(symbol) else "day")
        return await self.submit(mode, inp, source="manual", idempotency_key=idempotency_key)
