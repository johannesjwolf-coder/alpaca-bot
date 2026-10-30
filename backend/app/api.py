"""HTTP-API unter /api. Alle Antworten enthalten den Modus; keine Zugangsdaten."""

from __future__ import annotations

import platform
import sys
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .bot import BotStartError
from .broker.alpaca import AlpacaPaperBroker
from .broker.base import (
    FINAL_ORDER_STATUSES,
    LOCAL_FINAL,
    SYMBOL_RE,
    BrokerError,
    is_open_status,
)
from .broker.demo import DemoBroker
from .config import MARKET_DATA_BASE_URL, PAPER_TRADING_BASE_URL
from .connection_check import check_connection
from .core import AppContext
from .db import iso
from .orders import OrderBlocked, OrderFailed, OrderInput
from .settings_store import (
    BotParams,
    RiskLimits,
    get_bot_params,
    get_risk_limits,
    set_bot_params,
    set_risk_limits,
)

router = APIRouter(prefix="/api")

Mode = Literal["DEMO", "PAPER"]


def ctx_of(request: Request) -> AppContext:
    return request.app.state.ctx


def require_mode(ctx: AppContext, expected: str) -> None:
    """Schutz gegen Aktionen im falschen Modus (z. B. zweiter Browser-Tab)."""
    if expected != ctx.mode:
        raise HTTPException(
            409, f"Aktiver Modus ist {ctx.mode}, die Anfrage war für {expected}. Bitte Ansicht neu laden."
        )


def broker_http_error(exc: BrokerError) -> HTTPException:
    return HTTPException(502, {"message": exc.message, "kind": exc.kind, "hint": exc.hint})


# ---------------------------------------------------------------- Status
@router.get("/status")
async def status(request: Request) -> dict:
    ctx = ctx_of(request)
    rt = ctx.runtime
    snap = rt.snapshot
    return {
        "mode": ctx.mode,
        "server_time": iso(rt._now()),
        "orders_enabled": ctx.orders_enabled(ctx.mode),
        "bot": ctx.bot.state(),
        "connection": rt.status_dict(),
        "market": (
            {
                "is_open": snap.clock.is_open,
                "next_open": iso(snap.clock.next_open),
                "next_close": iso(snap.clock.next_close),
                "simulated": ctx.mode == "DEMO",
            }
            if snap
            else None
        ),
        "data_source": rt.broker.data_source_label,
    }


class ModeBody(BaseModel):
    mode: Mode


@router.post("/mode")
async def set_mode(body: ModeBody, request: Request) -> dict:
    ctx = ctx_of(request)
    await ctx.set_mode(body.mode)
    await ctx.runtime.sync()
    return await status(request)


@router.post("/sync")
async def sync_now(request: Request) -> dict:
    ctx = ctx_of(request)
    await ctx.runtime.sync()
    return ctx.runtime.status_dict()


@router.get("/connection-test")
async def connection_test(request: Request) -> dict:
    """Lesender Test GET /v2/account am Paper-Host – unabhängig vom aktiven Modus."""
    ctx = ctx_of(request)
    broker = ctx.runtimes["PAPER"].broker
    auth_mode = broker.auth.mode if isinstance(broker, AlpacaPaperBroker) else "test"
    return await check_connection(broker, auth_mode)


# ------------------------------------------------------------- Dashboard
def _merged_orders(ctx: AppContext, mode: str) -> list[dict]:
    rt = ctx.runtimes[mode]
    local = {
        r["client_order_id"]: r
        for r in ctx.db.query("SELECT * FROM orders WHERE mode=? ORDER BY id DESC LIMIT 200", (mode,))
    }
    out: dict[str, dict] = {}
    if rt.snapshot is not None:
        broker_orders = {o.id: o for o in [*rt.snapshot.recent_orders, *rt.snapshot.open_orders]}
        for o in broker_orders.values():
            row = local.get(o.client_order_id)
            out[o.client_order_id or o.id] = {
                "broker_order_id": o.id,
                "client_order_id": o.client_order_id,
                "symbol": o.symbol,
                "side": o.side,
                "type": o.type,
                "time_in_force": o.time_in_force,
                "qty": o.qty,
                "notional": o.notional,
                "filled_qty": o.filled_qty,
                "filled_avg_price": o.filled_avg_price,
                "status": o.status,
                "source": row["source"] if row else "extern",
                "created_at": iso(o.created_at or o.submitted_at),
                "updated_at": iso(o.updated_at),
                "cancelable": is_open_status(o.status) and o.status != "pending_cancel",
                "message": row["message"] if row else None,
                "from_broker": True,
            }
    for cid, r in local.items():
        if cid in out:
            continue
        out[cid] = {
            "broker_order_id": r["broker_order_id"],
            "client_order_id": cid,
            "symbol": r["symbol"],
            "side": r["side"],
            "type": "market",
            "time_in_force": r["time_in_force"],
            "qty": float(r["qty"]) if r["qty"] else None,
            "notional": float(r["notional"]) if r["notional"] else None,
            "filled_qty": r["filled_qty"],
            "filled_avg_price": r["filled_avg_price"],
            "status": r["status"],
            "source": r["source"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "cancelable": bool(r["broker_order_id"])
            and r["status"] not in FINAL_ORDER_STATUSES | LOCAL_FINAL,
            "message": r["message"],
            "from_broker": False,
        }
    return sorted(out.values(), key=lambda x: x["created_at"] or "", reverse=True)[:100]


@router.get("/dashboard")
async def dashboard(request: Request) -> dict:
    ctx = ctx_of(request)
    mode = ctx.mode
    rt = ctx.runtimes[mode]
    await rt.sync(max_age_s=ctx.settings.sync_interval_s / 2)
    snap = rt.snapshot if rt.connection == "ok" else None
    limits = get_risk_limits(ctx.db, mode)
    return {
        "mode": mode,
        "connection": rt.status_dict(),
        # Bei Fehlern werden KEINE (Demo-)Ersatzdaten geliefert.
        "account": snap.account.model_dump() if snap else None,
        "positions": [p.model_dump() for p in snap.positions] if snap else [],
        "orders": _merged_orders(ctx, mode),
        "fetched_at": iso(snap.fetched_at) if snap else None,
        "daily": ctx.risk.daily_status(mode, snap.account if snap else None, limits),
        "orders_today": ctx.risk.orders_today(mode),
        "limits": limits.model_dump(),
    }


@router.get("/bars")
async def bars(request: Request, symbol: str = Query("SPY", max_length=10)) -> dict:
    ctx = ctx_of(request)
    rt = ctx.runtime
    params = get_bot_params(ctx.db, ctx.mode)
    res = await ctx.market.bars(rt, symbol, limit=240, max_age_s=params.max_bar_age_s)
    if symbol.strip().upper() == params.symbol:
        res["strategy"] = {"short_window": params.short_window, "long_window": params.long_window}
    return res


# ---------------------------------------------------------------- Orders
class OrderBody(OrderInput):
    expected_mode: Mode
    idempotency_key: str | None = Field(None, min_length=8, max_length=80)
    confirm: bool = False


@router.post("/orders/preview")
async def order_preview(body: OrderBody, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    return await ctx.orders.preview(ctx.mode, OrderInput(**body.model_dump(include=set(OrderInput.model_fields))))


@router.post("/orders")
async def order_submit(body: OrderBody, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    if not body.confirm or not body.idempotency_key:
        raise HTTPException(400, "Bitte die Zusammenfassung prüfen und ausdrücklich bestätigen.")
    inp = OrderInput(**body.model_dump(include=set(OrderInput.model_fields)))
    try:
        return await ctx.orders.submit(
            ctx.mode, inp, source="manual", idempotency_key=f"ui:{body.idempotency_key}"
        )
    except OrderBlocked as exc:
        raise HTTPException(422, {"message": "Order durch Prüfungen gesperrt", "preview": exc.preview}) from exc
    except OrderFailed as exc:
        raise HTTPException(
            502, {"message": exc.error.message, "kind": exc.error.kind, "hint": exc.error.hint, "order": exc.order}
        ) from exc


class ModeOnly(BaseModel):
    expected_mode: Mode


@router.post("/orders/{order_id}/cancel")
async def order_cancel(order_id: str, body: ModeOnly, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    try:
        await ctx.orders.cancel(ctx.mode, order_id)
    except BrokerError as exc:
        raise broker_http_error(exc) from exc
    return {"ok": True}


@router.post("/orders/cancel-open")
async def orders_cancel_open(body: ModeOnly, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    try:
        return await ctx.orders.cancel_all_open(ctx.mode)
    except BrokerError as exc:
        raise broker_http_error(exc) from exc


class CloseBody(BaseModel):
    expected_mode: Mode
    idempotency_key: str = Field(min_length=8, max_length=80)
    confirm: bool = False


@router.post("/positions/{symbol}/close")
async def position_close(symbol: str, body: CloseBody, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise HTTPException(400, "Ungültiges Symbol")
    if not body.confirm:
        raise HTTPException(400, "Schliessen der Position bitte ausdrücklich bestätigen.")
    try:
        return await ctx.orders.close_position(ctx.mode, symbol, f"close:{body.idempotency_key}")
    except OrderBlocked as exc:
        raise HTTPException(422, {"message": "Schliessen gesperrt", "preview": exc.preview}) from exc
    except OrderFailed as exc:
        raise HTTPException(502, {"message": exc.error.message, "kind": exc.error.kind}) from exc
    except BrokerError as exc:
        raise broker_http_error(exc) from exc


class ReleaseBody(BaseModel):
    expected_mode: Mode
    enabled: bool


@router.post("/orders-release")
async def orders_release(body: ReleaseBody, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    if ctx.mode != "PAPER":
        raise HTTPException(400, "Die Paper-Orderfreigabe gibt es nur im PAPER-Modus. DEMO-Orders sind simuliert.")
    if not body.enabled and ctx.bot.running:
        await ctx.bot.pause("Orderfreigabe deaktiviert")
    ctx.set_orders_enabled(ctx.mode, body.enabled)
    return {"mode": ctx.mode, "orders_enabled": ctx.orders_enabled(ctx.mode)}


# ------------------------------------------------------------ Einstellungen
@router.get("/settings")
async def get_settings(request: Request) -> dict:
    ctx = ctx_of(request)
    return {
        "mode": ctx.mode,
        "risk_limits": get_risk_limits(ctx.db, ctx.mode).model_dump(),
        "bot_params": get_bot_params(ctx.db, ctx.mode).model_dump(),
        "defaults": {"risk_limits": RiskLimits().model_dump(), "bot_params": BotParams().model_dump()},
        "note": "Standardwerte sind kleine technische Paper-Testwerte, keine Empfehlung.",
    }


class RiskBody(RiskLimits):
    expected_mode: Mode


class BotBody(BotParams):
    expected_mode: Mode


@router.put("/settings/risk")
async def put_risk(body: RiskBody, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    limits = RiskLimits(**body.model_dump(exclude={"expected_mode"}))
    set_risk_limits(ctx.db, ctx.mode, limits)
    ctx.db.add_event(ctx.mode, "info", "settings", "Risikogrenzen geändert.", limits.model_dump())
    return await get_settings(request)


@router.put("/settings/bot")
async def put_bot(body: BotBody, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    if ctx.bot.running:
        raise HTTPException(409, "Strategieparameter nur bei pausiertem Bot ändern.")
    try:
        params = BotParams(**body.model_dump(exclude={"expected_mode"}))
    except ValidationError as exc:  # pragma: no cover - bereits durch FastAPI validiert
        raise HTTPException(422, str(exc)) from exc
    set_bot_params(ctx.db, ctx.mode, params)
    ctx.db.add_event(ctx.mode, "info", "settings", "Strategieparameter geändert.", params.model_dump())
    return await get_settings(request)


# -------------------------------------------------------------------- Bot
@router.post("/bot/start")
async def bot_start(body: ModeOnly, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    try:
        return await ctx.bot.start(ctx.mode)
    except BotStartError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/bot/pause")
async def bot_pause(request: Request) -> dict:
    return await ctx_of(request).bot.pause("Vom Nutzer pausiert")


@router.get("/bot")
async def bot_state(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict:
    ctx = ctx_of(request)
    return {
        "mode": ctx.mode,
        "state": ctx.bot.state(),
        "params": get_bot_params(ctx.db, ctx.mode).model_dump(),
        "decisions": ctx.db.list_decisions(ctx.mode, limit),
        "disclaimer": (
            "Beispielstrategie (Kreuzung zweier gleitender Durchschnitte auf abgeschlossenen Minutenkerzen) "
            "nur zum Prüfen der Automatik – weder validiert noch auf Gewinn ausgelegt."
        ),
    }


@router.get("/events")
async def events(request: Request, limit: int = Query(100, ge=1, le=500)) -> dict:
    ctx = ctx_of(request)
    return {"mode": ctx.mode, "events": ctx.db.list_events(ctx.mode, limit)}


@router.post("/demo/reset")
async def demo_reset(body: ModeOnly, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    if ctx.mode != "DEMO":
        raise HTTPException(400, "Nur im DEMO-Modus möglich.")
    await ctx.bot.pause("DEMO-Konto zurückgesetzt")
    broker = ctx.runtimes["DEMO"].broker
    if isinstance(broker, DemoBroker):
        broker.reset()
    ctx.db.execute("DELETE FROM orders WHERE mode='DEMO'")
    ctx.db.execute("DELETE FROM decisions WHERE mode='DEMO'")
    ctx.db.execute("DELETE FROM kv WHERE mode='DEMO' AND (key='daily_risk' OR key LIKE 'bot_last_bar:%')")
    ctx.db.add_event("DEMO", "info", "system", "DEMO-Konto zurückgesetzt (simulierte Daten).")
    ctx.market.clear()
    await ctx.runtime.sync()
    return {"ok": True}


# -------------------------------------------------------------- Diagnose
@router.get("/diagnostics")
async def diagnostics(request: Request) -> dict:
    ctx = ctx_of(request)
    paper = ctx.runtimes["PAPER"].broker
    auth = paper.auth.describe() if isinstance(paper, AlpacaPaperBroker) else {"auth_mode": "test"}
    s = ctx.settings
    return {
        "app_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(terse=True),
        "mode": ctx.mode,
        "started_at": iso(ctx.started_at),
        "auth": auth,  # nur Ja/Nein-Angaben, nie Werte
        "endpoints": {"trading": PAPER_TRADING_BASE_URL, "market_data": MARKET_DATA_BASE_URL, "feed": "iex"},
        "binding": {"host": s.host, "port": s.port, "loopback_only": s.binds_loopback},
        "allowed_origins": s.allowed_origins,
        "db_path": str(s.db_path),
        "runtimes": {m: rt.status_dict() for m, rt in ctx.runtimes.items()},
        "bot": ctx.bot.state(),
        "single_process": "Erste Version: genau ein Backend-Prozess (Dateisperre aktiv beim Start über python -m app).",
    }
