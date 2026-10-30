"""Stufe 3: Push-Benachrichtigungen (ntfy, gemockt) und optionales Fortsetzen nach Neustart."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.broker.alpaca import AlpacaPaperBroker, EnvAuth
from app.core import AppContext
from app.main import create_app
from app.notify import Notifier, NotifyConfigError, NtfyConfig, classify
from app.settings_store import BotParams, NotifySettings, set_bot_params, set_notify_settings

UI = {"X-Requested-With": "alpaca-bot-ui"}
TOPIC, TOKEN = "bot-topic_1", "tok-123"
SCALP = dict(strategy="scalp", trend_filter=False, symbol="BTC/USD", symbols=["BTC/USD"], short_window=2, long_window=4, bar_seconds=5,
             poll_interval_s=1, order_value=20, broker_stop=False)


@pytest.fixture
def ntfy(ctx: AppContext, clock):
    sent: list[tuple[httpx.Request, dict]] = []
    status = {"code": 200}

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append((req, json.loads(req.content)))
        return httpx.Response(status["code"], json={})

    n = Notifier(ctx, NtfyConfig("https://ntfy.example", TOPIC, TOKEN), transport=httpx.MockTransport(handler), now=clock)
    ctx.db.event_listeners[:] = [n.on_event]
    ctx.notifier = n
    n.sent, n.status = sent, status  # type: ignore[attr-defined]
    return n


def bodies(n) -> list[dict]:
    return [b for _, b in n.sent]


# ------------------------------------------------------------ Konfiguration
def test_ntfy_config_validation(monkeypatch):
    assert NtfyConfig.from_env() is None
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    cfg = NtfyConfig.from_env()
    assert cfg is not None and cfg.url == "https://ntfy.sh" and cfg.token is None
    assert NtfyConfig.validated("http://127.0.0.1:8080", TOPIC, None).host == "127.0.0.1"
    for url in ("http://ntfy.sh", "https://user:pw@ntfy.sh", "https://ntfy.sh/?x=1", "ftp://ntfy.sh"):
        with pytest.raises(NotifyConfigError):
            NtfyConfig.validated(url, TOPIC, None)
    with pytest.raises(NotifyConfigError):
        NtfyConfig.validated("https://ntfy.sh", "mit leerzeichen", None)


def test_classify_rules():
    assert classify("info", "trade", False)[0] == "trades"
    assert classify("warning", "risk", False)[0] == "warnings"
    assert classify("error", "order", False)[1] == 4
    assert classify("info", "bot", False) is None  # Statuswechsel nur mit push
    assert classify("info", "bot", True)[0] == "bot"
    assert classify("info", "order", True) is None
    assert classify("info", "settings", False) is None


# ------------------------------------------------------------ Versand
async def test_trade_event_is_pushed_without_leaking_secrets(paper_ctx: AppContext, ntfy):
    paper_ctx.db.add_event("PAPER", "info", "trade", "Einstieg ausgeführt: 0.001 BTC/USD zu 100.00.")
    await ntfy.flush()
    trade_msgs = [(r, b) for r, b in ntfy.sent if "Einstieg" in b["message"]]
    assert len(trade_msgs) == 1
    req, body = trade_msgs[0]
    assert str(req.url) == "https://ntfy.example/"
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert body["topic"] == TOPIC and body["title"] == "PAPER · Trade" and body["priority"] == 3
    described = json.dumps(ntfy.describe())
    assert TOPIC not in described and TOKEN not in described
    assert ntfy.describe()["server"] == "ntfy.example" and ntfy.describe()["token_set"] is True


async def test_demo_events_only_when_not_paper_only(ctx: AppContext, ntfy):
    ctx.db.add_event("DEMO", "info", "trade", "Demo-Trade A")
    await ntfy.flush()
    assert not any("Demo-Trade" in b["message"] for b in bodies(ntfy))
    set_notify_settings(ctx.db, NotifySettings(paper_only=False))
    ctx.db.add_event("DEMO", "info", "trade", "Demo-Trade B")
    await ntfy.flush()
    assert [b["title"] for b in bodies(ntfy) if "Demo-Trade" in b["message"]] == ["DEMO · Trade"]


async def test_categories_can_be_switched_off(paper_ctx: AppContext, ntfy):
    set_notify_settings(paper_ctx.db, NotifySettings(trades=False))
    paper_ctx.db.add_event("PAPER", "info", "trade", "Trade X geschlossen")
    paper_ctx.db.add_event("PAPER", "warning", "risk", "Tagesverlustgrenze erreicht")
    await ntfy.flush()
    msgs = [b["message"] for b in bodies(ntfy)]
    assert "Tagesverlustgrenze erreicht" in msgs and "Trade X geschlossen" not in msgs
    set_notify_settings(paper_ctx.db, NotifySettings(enabled=False))
    paper_ctx.db.add_event("PAPER", "error", "order", "Broker lehnte ab")
    await ntfy.flush()
    assert "Broker lehnte ab" not in [b["message"] for b in bodies(ntfy)]


async def test_dedup_and_rate_limit(paper_ctx: AppContext, ntfy, clock):
    for _ in range(3):
        paper_ctx.db.add_event("PAPER", "warning", "bot", "Getrennt: Zeitüberschreitung")
    for i in range(15):
        paper_ctx.db.add_event("PAPER", "warning", "risk", f"Warnung {i}")
    await ntfy.flush()
    msgs = [b["message"] for b in bodies(ntfy)]
    assert msgs.count("Getrennt: Zeitüberschreitung") == 1
    assert len(msgs) == 12 and ntfy.dropped == 4  # 16 Meldungen in einer Minute, Grenze 12
    clock.advance(seconds=61)
    paper_ctx.db.add_event("PAPER", "warning", "risk", "Nach der Pause")
    await ntfy.flush()
    assert "+4 weitere Meldung(en)" in bodies(ntfy)[-1]["message"] and ntfy.dropped == 0


async def test_delivery_errors_are_recorded_not_raised(paper_ctx: AppContext, ntfy):
    ntfy.status["code"] = 403
    paper_ctx.db.add_event("PAPER", "warning", "risk", "Kaufpause")
    await ntfy.flush()
    assert ntfy.last_error == "ntfy antwortete mit HTTP 403" and ntfy.sent_total == 0
    assert TOPIC not in ntfy.last_error


async def test_bot_start_and_pause_are_pushed(paper_ctx: AppContext, ntfy, fake):
    set_bot_params(paper_ctx.db, "PAPER", BotParams(**SCALP))
    fake.set_quote("BTC/USD", 99.99, 100.01)
    await paper_ctx.bot.start("PAPER")
    await paper_ctx.bot.pause()
    await ntfy.flush()
    titles = [(b["title"], b["message"][:14]) for b in bodies(ntfy)]
    assert ("PAPER · Automatik", "Bot gestartet:") in titles and ("PAPER · Automatik", "Bot pausiert (") in titles


async def test_daily_summary_once_per_day(paper_ctx: AppContext, ntfy, clock):
    # T0 = 14:30 UTC = 15:30 Europe/Zurich
    set_notify_settings(paper_ctx.db, NotifySettings(summary_time="16:00"))
    assert ntfy.summary_due() is None
    clock.advance(minutes=31)
    assert ntfy.maybe_send_summary() is True
    assert ntfy.maybe_send_summary() is False
    await ntfy.flush()
    summary = [b for b in bodies(ntfy) if "Tageszusammenfassung" in b["title"]]
    assert len(summary) == 1 and "Bot-Trades heute: 0" in summary[0]["message"]


def test_notify_api(settings, paper_broker):
    app = create_app(settings, context_factory=lambda s, db: AppContext(s, db, paper_broker=paper_broker), background=False)
    with TestClient(app) as c:
        info = c.get("/api/settings").json()["notify"]
        assert info["configured"] is False and info["settings"]["paper_only"] is True
        assert c.put("/api/settings/notify", json={**info["settings"], "trades": False}).status_code == 403  # ohne CSRF-Kopf
        r = c.put("/api/settings/notify", headers=UI, json={**info["settings"], "trades": False, "summary_time": "07:15"})
        assert r.status_code == 200 and r.json()["settings"]["trades"] is False
        assert c.put("/api/settings/notify", headers=UI, json={**info["settings"], "summary_time": "25:00"}).status_code == 422
        r = c.post("/api/notify/test", headers=UI)
        assert r.json()["ok"] is False and "NTFY_TOPIC" in r.json()["error"]
        assert c.post("/api/notify/test", headers=UI).status_code == 429


# ------------------------------------------------------------ Fortsetzen
async def start_scalp(ctx: AppContext, fake) -> None:
    set_bot_params(ctx.db, "PAPER", BotParams(**SCALP))
    fake.set_quote("BTC/USD", 99.99, 100.01)
    await ctx.bot.start("PAPER")


def restarted(settings, db, fake, clock) -> AppContext:
    broker = AlpacaPaperBroker(EnvAuth(), transport=httpx.MockTransport(fake.handler), backoff_base_s=0.001)
    return AppContext(settings, db, paper_broker=broker, now=clock)


async def test_run_state_is_remembered(paper_ctx: AppContext, fake):
    assert paper_ctx.resume_enabled() is False
    await start_scalp(paper_ctx, fake)
    assert paper_ctx.resume_state()["bot"] is True and paper_ctx.resume_state()["mode"] == "PAPER"
    await paper_ctx.bot.pause("Anwendung wird beendet", remember=False)
    assert paper_ctx.resume_state()["bot"] is True  # Herunterfahren vergisst nichts
    await start_scalp(paper_ctx, fake)
    await paper_ctx.bot.pause()  # Pause durch den Nutzer
    assert paper_ctx.resume_state()["bot"] is False


async def test_default_restart_keeps_everything_off(paper_ctx: AppContext, fake, settings, db, clock):
    await start_scalp(paper_ctx, fake)
    await paper_ctx.shutdown()
    ctx2 = restarted(settings, db, fake, clock)
    await ctx2.startup(background=True)
    try:
        assert ctx2._resume_task is None and not ctx2.bot.running
        assert ctx2.mode == "DEMO" and not ctx2.orders_enabled("PAPER")
        assert "nach dem Neustart aus" in db.list_events("DEMO", 1)[0]["message"]
    finally:
        await ctx2.shutdown()


async def test_resume_after_restart_when_enabled(paper_ctx: AppContext, fake, settings, db, clock):
    paper_ctx.set_resume_enabled(True)
    await start_scalp(paper_ctx, fake)
    await paper_ctx.shutdown()
    ctx2 = restarted(settings, db, fake, clock)
    await ctx2.startup(background=True)
    try:
        assert ctx2._resume_task is not None
        assert await asyncio.wait_for(ctx2._resume_task, 5) is True
        assert ctx2.mode == "PAPER" and ctx2.orders_enabled("PAPER") and ctx2.bot.running
        msgs = [e["message"] for e in db.list_events("PAPER", 10)]
        assert "Automatik nach Neustart automatisch fortgesetzt." in msgs
        assert any("(Fortsetzen nach Neustart)" in m for m in msgs)
    finally:
        await ctx2.shutdown()


async def test_resume_gives_up_and_turns_release_off(paper_ctx: AppContext, fake, settings, db, clock):
    paper_ctx.set_resume_enabled(True)
    await start_scalp(paper_ctx, fake)
    await paper_ctx.shutdown()
    fake.crypto_quotes.clear()  # nach dem Neustart kein Kurs → Start scheitert
    ctx2 = restarted(settings, db, fake, clock)
    await ctx2.startup(background=False)
    try:
        assert await ctx2.resume_after_restart(ctx2.resume_state(), retry_s=0) is False
        assert not ctx2.bot.running and not ctx2.orders_enabled("PAPER")
        assert ctx2.resume_info()["status"].startswith("Fehlgeschlagen")
        assert any(e["level"] == "error" and "Fortsetzen nach Neustart fehlgeschlagen" in e["message"]
                   for e in db.list_events("PAPER", 20))
    finally:
        await ctx2.shutdown()


def test_resume_api_requires_confirmation(settings, paper_broker):
    app = create_app(settings, context_factory=lambda s, db: AppContext(s, db, paper_broker=paper_broker), background=False)
    with TestClient(app) as c:
        assert c.get("/api/status").json()["resume_after_restart"] is False
        assert c.put("/api/settings/resume", headers=UI, json={"enabled": True}).status_code == 400
        r = c.put("/api/settings/resume", headers=UI, json={"enabled": True, "confirm": True})
        assert r.status_code == 200 and r.json()["enabled"] is True
        assert c.get("/api/status").json()["resume_after_restart"] is True
        assert c.put("/api/settings/resume", headers=UI, json={"enabled": False}).json()["enabled"] is False
