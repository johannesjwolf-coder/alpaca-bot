"""HTTP-API unter /api. Alle Antworten enthalten den Modus; keine Zugangsdaten."""

from __future__ import annotations

import asyncio
import json
import platform
import sys
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .backtest import BacktestRequest, run_backtest
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
from .config import (
    CRYPTO_STREAM_URL,
    LOADED_ENV_FILES,
    MARKET_DATA_BASE_URL,
    PAPER_TRADING_BASE_URL,
    TRADE_STREAM_URL,
)
from .connection_check import check_connection
from .core import AppContext
from .db import iso
from .orders import OrderBlocked, OrderFailed, OrderInput
from .settings_store import (
    BotParams,
    NotifySettings,
    RiskLimits,
    get_bot_params,
    get_risk_limits,
    set_bot_params,
    set_notify_settings,
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
        "resume_after_restart": ctx.resume_enabled(),
        "notify_configured": ctx.notifier.configured,
        "streams": ctx.streams.describe(),
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
                "filled_at": iso(o.filled_at),
                "stop_price": o.stop_price,
                "limit_price": o.limit_price,
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
            "type": r.get("order_type") or "market",
            "time_in_force": r["time_in_force"],
            "qty": float(r["qty"]) if r["qty"] else None,
            "notional": float(r["notional"]) if r["notional"] else None,
            "filled_qty": r["filled_qty"],
            "filled_avg_price": r["filled_avg_price"],
            "status": r["status"],
            "source": r["source"],
            "created_at": r["created_at"],
            "filled_at": None,
            "stop_price": r.get("stop_price"),
            "limit_price": r.get("limit_price"),
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
async def bars(
    request: Request,
    symbol: str = Query("SPY", max_length=16),
    timeframe: str = Query("1Min", pattern="^(1Min|5Min|15Min|1Hour|1Day)$"),
    limit: int = Query(300, ge=20, le=1000),
) -> dict:
    ctx = ctx_of(request)
    rt = ctx.runtime
    params = get_bot_params(ctx.db, ctx.mode)
    res = await ctx.market.bars(rt, symbol, limit=limit, max_age_s=params.max_bar_age_s, timeframe=timeframe)
    if symbol.strip().upper() == params.symbol and params.strategy == "sma" and timeframe == "1Min":
        session_open = None
        if rt.broker.uses_sessions:
            try:
                session_open = iso(await rt.broker.get_session_open(rt._now()))
            except BrokerError:
                session_open = None
        res["strategy"] = {
            "short_window": params.short_window,
            "long_window": params.long_window,
            "session_open": session_open,
            "session_filter": rt.broker.uses_sessions,
        }
    return res


@router.get("/symbols")
async def symbols(request: Request, q: str = Query("", max_length=24)) -> dict:
    ctx = ctx_of(request)
    return await ctx.symbols.suggest(ctx.mode, q)


# ---------------------------------------------------------------- Orders
class OrderBody(OrderInput):
    expected_mode: Mode
    idempotency_key: str | None = Field(None, min_length=8, max_length=80)
    confirm: bool = False


def _manual_market_only(body: OrderBody) -> None:
    if body.type != "market":
        raise HTTPException(400, "Manuelle Orders sind in dieser Version Market-Orders.")


@router.post("/orders/preview")
async def order_preview(body: OrderBody, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    _manual_market_only(body)
    return await ctx.orders.preview(ctx.mode, OrderInput(**body.model_dump(include=set(OrderInput.model_fields))))


@router.post("/orders")
async def order_submit(body: OrderBody, request: Request) -> dict:
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    _manual_market_only(body)
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


@router.post("/positions/{symbol:path}/close")
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
        "notify": ctx.notifier.describe(),
        "resume": ctx.resume_info(),
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


@router.put("/settings/notify")
async def put_notify(body: NotifySettings, request: Request) -> dict:
    ctx = ctx_of(request)
    set_notify_settings(ctx.db, body)
    ctx.db.add_event(ctx.mode, "info", "settings", "Benachrichtigungs-Einstellungen geändert.", body.model_dump())
    return ctx.notifier.describe()


@router.post("/notify/test")
async def notify_test(request: Request) -> dict:
    ctx = ctx_of(request)
    last = ctx.notifier.last_test_at
    if last is not None and (ctx._now() - last).total_seconds() < 10:
        raise HTTPException(429, "Bitte einige Sekunden bis zur nächsten Testnachricht warten.")
    ctx.notifier.last_test_at = ctx._now()
    return await ctx.notifier.send_test()


class ResumeBody(BaseModel):
    enabled: bool
    confirm: bool = False


@router.put("/settings/resume")
async def put_resume(body: ResumeBody, request: Request) -> dict:
    ctx = ctx_of(request)
    if body.enabled and not body.confirm:
        raise HTTPException(400, "Fortsetzen nach Neustart muss ausdrücklich bestätigt werden.")
    ctx.set_resume_enabled(body.enabled)
    return ctx.resume_info()


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
    params = get_bot_params(ctx.db, ctx.mode)
    return {
        "mode": ctx.mode,
        "state": ctx.bot.state(),
        "params": params.model_dump(),
        "roundtrip_cost_pct": params.roundtrip_cost_pct,
        "defaults": {
            "scalp": BotParams().model_dump(),
            "sma": BotParams(strategy="sma", symbol="SPY", short_window=10, long_window=30, order_value=20).model_dump(),
        },
        "trades": ctx.bot.scalper.list_trades(ctx.mode, 50),
        "summary": ctx.bot.scalper.summary(ctx.mode),
        "decisions": ctx.db.list_decisions(ctx.mode, limit),
        "disclaimer": (
            "Beispielstrategie (Kreuzung zweier gleitender Durchschnitte auf abgeschlossenen Minutenkerzen) "
            "nur zum Prüfen der Automatik – weder validiert noch auf Gewinn ausgelegt."
        ),
    }


@router.post("/emergency-stop")
async def emergency_stop(body: ModeOnly, request: Request) -> dict:
    """Not-Aus: Bot stoppen, alle offenen Orders stornieren, alle Positionen schliessen.

    Das Schliessen sendet Verkaufsorders und braucht im PAPER-Modus die Orderfreigabe.
    """
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    mode = ctx.mode
    await ctx.bot.pause("Not-Aus")
    ctx.db.add_event(mode, "warning", "system", "NOT-AUS ausgelöst.")
    result: dict = {"mode": mode, "bot_paused": True, "canceled": [], "closed": [], "not_closed": []}
    try:
        result["canceled"] = (await ctx.orders.cancel_all_open(mode))["results"]
    except BrokerError as exc:
        result["cancel_error"] = exc.message
    rt = ctx.runtimes[mode]
    if not await rt.sync() or rt.snapshot is None:
        result["error"] = "Brokerzustand nicht abrufbar – Positionen konnten nicht geschlossen werden."
        return result
    can_sell = mode == "DEMO" or ctx.orders_enabled(mode)
    for pos in rt.snapshot.positions:
        if pos.qty <= 0:
            continue
        if not can_sell:
            result["not_closed"].append({"symbol": pos.symbol, "reason": "Paper-Orderfreigabe ist aus"})
            continue
        try:
            res = await ctx.orders.close_position(mode, pos.symbol, f"kill:{uuid.uuid4().hex}")
            result["closed"].append({"symbol": pos.symbol, "status": res["order"]["status"]})
        except OrderBlocked as exc:
            reasons = [c["detail"] for c in exc.preview.get("checks", []) if c["blocking"] and not c["ok"]]
            result["not_closed"].append({"symbol": pos.symbol, "reason": "; ".join(reasons) or "gesperrt"})
        except (OrderFailed, BrokerError) as exc:
            msg = exc.error.message if isinstance(exc, OrderFailed) else exc.message
            result["not_closed"].append({"symbol": pos.symbol, "reason": msg})
    for trade in ctx.db.query("SELECT id FROM trades WHERE mode=? AND status='open'", (mode,)):
        ctx.db.execute(
            "UPDATE trades SET status='closed', exit_reason='Not-Aus', note=?, updated_at=? WHERE id=?",
            ("Durch Not-Aus geschlossen – Ergebnis siehe Auftragstabelle", iso(ctx._now()), trade["id"]),
        )
    ctx.db.add_event(
        mode, "warning", "system",
        f"Not-Aus: {len(result['canceled'])} Order(s) storniert, {len(result['closed'])} Position(en) geschlossen, "
        f"{len(result['not_closed'])} nicht geschlossen.",
    )
    return result


# --------------------------------------------------------------- Backtest
class BacktestBody(BacktestRequest):
    expected_mode: Mode


_backtest_lock = asyncio.Lock()
KEEP_BACKTESTS = 30


@router.post("/backtests")
async def backtest_run(body: BacktestBody, request: Request) -> dict:
    """Backtest auf historischen Kerzen – sendet keine Orders, liest nur Marktdaten."""
    ctx = ctx_of(request)
    require_mode(ctx, body.expected_mode)
    req = BacktestRequest(**body.model_dump(exclude={"expected_mode"}))
    if _backtest_lock.locked():
        raise HTTPException(409, "Es läuft bereits ein Backtest.")
    async with _backtest_lock:
        try:
            result = await run_backtest(ctx, ctx.mode, req, get_risk_limits(ctx.db, ctx.mode))
        except BrokerError as exc:
            raise broker_http_error(exc) from exc
    cur = ctx.db.execute(
        "INSERT INTO backtests (mode, created_at, symbol, strategy, request, metrics, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ctx.mode, result["created_at"], req.symbol, req.strategy, json.dumps(req.model_dump()),
         json.dumps(result["metrics"]), json.dumps(result)),
    )
    ctx.db.execute(
        "DELETE FROM backtests WHERE mode=? AND id NOT IN (SELECT id FROM backtests WHERE mode=? ORDER BY id DESC LIMIT ?)",
        (ctx.mode, ctx.mode, KEEP_BACKTESTS),
    )
    result["id"] = cur.lastrowid
    ctx.db.add_event(
        ctx.mode, "info", "backtest",
        f"Backtest {req.symbol} ({req.strategy}, {req.days} Tage): {result['metrics']['trades']} Trades, "
        f"netto {result['metrics']['net_pnl']:+.2f} USD.",
    )
    return result


@router.get("/backtests")
async def backtest_list(request: Request) -> dict:
    ctx = ctx_of(request)
    rows = ctx.db.query(
        "SELECT id, created_at, symbol, strategy, request, metrics FROM backtests WHERE mode=? ORDER BY id DESC",
        (ctx.mode,),
    )
    for r in rows:
        r["request"] = json.loads(r["request"])
        r["metrics"] = json.loads(r["metrics"])
    return {"mode": ctx.mode, "runs": rows}


@router.get("/backtests/{run_id}")
async def backtest_get(run_id: int, request: Request) -> dict:
    ctx = ctx_of(request)
    row = ctx.db.query_one("SELECT id, result FROM backtests WHERE mode=? AND id=?", (ctx.mode, run_id))
    if not row:
        raise HTTPException(404, "Backtest nicht gefunden")
    result = json.loads(row["result"])
    result["id"] = row["id"]
    return result


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
        "env_files": LOADED_ENV_FILES or ["keine .env gefunden"],
        "endpoints": {
            "trading": PAPER_TRADING_BASE_URL, "market_data": MARKET_DATA_BASE_URL, "feed": "iex",
            "crypto_stream": CRYPTO_STREAM_URL, "order_stream": TRADE_STREAM_URL,
        },
        "streams": ctx.streams.describe(),
        "binding": {"host": s.host, "port": s.port, "loopback_only": s.binds_loopback},
        "allowed_origins": s.allowed_origins,
        "db_path": str(s.db_path),
        "runtimes": {m: rt.status_dict() for m, rt in ctx.runtimes.items()},
        "bot": ctx.bot.state(),
        "single_process": "Erste Version: genau ein Backend-Prozess (Dateisperre aktiv beim Start über python -m app).",
    }
