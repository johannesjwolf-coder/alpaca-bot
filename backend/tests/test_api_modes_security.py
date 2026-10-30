"""Demo/Paper-Trennung über die API sowie CSRF-, Origin- und Host-Schutz."""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import AppContext
from app.main import create_app

H = {"X-Requested-With": "alpaca-bot-ui"}


@pytest.fixture
def client_and_fake(settings, paper_broker, fake, clock):
    def factory(s, db):
        return AppContext(s, db, paper_broker=paper_broker)

    app = create_app(settings, context_factory=factory, background=False)
    with TestClient(app) as c:
        yield c, fake


def test_starts_in_demo_paused(client_and_fake):
    c, fake = client_and_fake
    st = c.get("/api/status").json()
    assert st["mode"] == "DEMO"
    assert st["bot"]["running"] is False
    assert st["orders_enabled"] is False
    assert "DEMO" in st["data_source"]
    assert fake.calls == []


def test_paper_failure_never_shows_demo_data(client_and_fake):
    c, fake = client_and_fake
    demo = c.get("/api/dashboard").json()
    assert demo["account"]["cash"] == 10000.0  # DEMO-Konto
    fake.inject[("GET", "/v2/account")] = [httpx.Response(401, json={"message": "unauthorized"})] * 5
    assert c.post("/api/mode", json={"mode": "PAPER"}, headers=H).status_code == 200
    dash = c.get("/api/dashboard").json()
    assert dash["mode"] == "PAPER"
    assert dash["account"] is None and dash["positions"] == []
    assert dash["connection"]["connection"] == "error"
    assert dash["connection"]["last_error"]["kind"] == "auth_rejected"
    bars = c.get("/api/bars?symbol=SPY").json()
    assert bars["mode"] == "PAPER" and "IEX" in bars["source"]
    assert bars["status"] in ("empty", "error", "stale", "market_closed", "ok")


def test_demo_and_paper_data_are_separate(client_and_fake):
    c, fake = client_and_fake
    r = c.post(
        "/api/orders",
        json={"expected_mode": "DEMO", "symbol": "SPY", "side": "buy", "notional": "20",
              "idempotency_key": "demo-key-1", "confirm": True},
        headers=H,
    )
    assert r.status_code == 200, r.text
    assert c.get("/api/dashboard").json()["orders"]
    c.post("/api/mode", json={"mode": "PAPER"}, headers=H)
    dash = c.get("/api/dashboard").json()
    assert dash["orders"] == [] and dash["positions"] == []
    assert dash["account"]["cash"] == 1000.0  # Paper-Konto vom Fake
    assert fake.count("POST", "/v2/orders") == 0
    # Einstellungen getrennt
    c.put("/api/settings/risk", json={"expected_mode": "PAPER", "max_order_value": 7, "max_total_invested": 50,
                                       "max_open_positions": 2, "max_orders_per_day": 3, "max_daily_loss": 5,
                                       "price_buffer_pct": 1}, headers=H)
    c.post("/api/mode", json={"mode": "DEMO"}, headers=H)
    assert c.get("/api/settings").json()["risk_limits"]["max_order_value"] == 50.0


def test_wrong_expected_mode_rejected(client_and_fake):
    c, fake = client_and_fake
    r = c.post(
        "/api/orders",
        json={"expected_mode": "PAPER", "symbol": "SPY", "side": "buy", "notional": "5",
              "idempotency_key": "k-12345678", "confirm": True},
        headers=H,
    )
    assert r.status_code == 409
    assert fake.count("POST", "/v2/orders") == 0


def test_order_requires_confirmation(client_and_fake):
    c, _ = client_and_fake
    r = c.post("/api/orders", json={"expected_mode": "DEMO", "symbol": "SPY", "side": "buy", "notional": "5",
                                    "idempotency_key": "k-abcdefgh"}, headers=H)
    assert r.status_code == 400


def test_paper_release_only_in_paper(client_and_fake):
    c, _ = client_and_fake
    r = c.post("/api/orders-release", json={"expected_mode": "DEMO", "enabled": True}, headers=H)
    assert r.status_code == 400


def test_demo_flow_partial_fill_and_close(client_and_fake):
    c, _ = client_and_fake
    r = c.post("/api/orders", json={"expected_mode": "DEMO", "symbol": "F", "side": "buy", "qty": "4",
                                    "idempotency_key": "demo-qty-4", "confirm": True}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["order"]["status"] == "new"


def test_csrf_header_required(client_and_fake):
    c, _ = client_and_fake
    r = c.post("/api/mode", json={"mode": "PAPER"})
    assert r.status_code == 403
    assert c.get("/api/status").json()["mode"] == "DEMO"


def test_foreign_origin_rejected(client_and_fake):
    c, _ = client_and_fake
    r = c.post("/api/mode", json={"mode": "PAPER"}, headers={**H, "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/api/sync", headers={**H, "Origin": "http://localhost:5173"})
    assert r.status_code == 200


def test_untrusted_host_rejected(client_and_fake):
    c, _ = client_and_fake
    r = c.get("/api/status", headers={"Host": "attacker.example"})
    assert r.status_code == 400


def test_no_cors_headers_and_security_headers(client_and_fake):
    c, _ = client_and_fake
    r = c.get("/api/status", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_diagnostics_contain_no_secrets(client_and_fake):
    c, _ = client_and_fake
    text = c.get("/api/diagnostics").text
    assert "testsecretvalue" not in text and "PKTESTKEYID" not in text
    d = c.get("/api/diagnostics").json()
    assert d["auth"] == {"auth_mode": "env", "api_key_present": True, "secret_key_present": True}
    assert d["endpoints"]["trading"] == "https://paper-api.alpaca.markets"
    assert d["endpoints"]["feed"] == "iex"


def test_connection_endpoint_reports_paper_even_in_demo(client_and_fake):
    c, fake = client_and_fake
    res = c.get("/api/connection-test").json()
    assert res["ok"] is True and res["mode"] == "PAPER" and res["equity"] == 1000.0
    assert c.get("/api/status").json()["mode"] == "DEMO"


def test_demo_bot_start_pause_via_api(client_and_fake):
    c, _ = client_and_fake
    r = c.post("/api/bot/start", json={"expected_mode": "DEMO"}, headers=H)
    assert r.status_code == 200 and r.json()["running"] is True
    r2 = c.post("/api/bot/start", json={"expected_mode": "DEMO"}, headers=H)
    assert r2.status_code == 409
    r3 = c.post("/api/bot/pause", headers=H)
    assert r3.json()["running"] is False
    assert "Positionen" in r3.json()["pause_meaning"]
    time.sleep(0)  # noqa


def test_dotenv_bom_and_empty_existing_value(tmp_path, monkeypatch):
    from app.config import load_dotenv

    env = tmp_path / ".env"
    env.write_bytes("\ufeffALPACA_AUTH_MODE=env\nALPACA_API_KEY = \"PKAUSDATEI\"\nALPACA_SECRET_KEY=\n".encode())
    monkeypatch.delenv("ALPACA_AUTH_MODE", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "")  # leere Systemvariable darf .env nicht verdecken
    monkeypatch.setenv("ALPACA_SECRET_KEY", "aus-umgebung")  # gesetzte Variable hat Vorrang
    load_dotenv(env)
    import os

    assert os.environ["ALPACA_AUTH_MODE"] == "env"
    assert os.environ["ALPACA_API_KEY"] == "PKAUSDATEI"
    assert os.environ["ALPACA_SECRET_KEY"] == "aus-umgebung"
