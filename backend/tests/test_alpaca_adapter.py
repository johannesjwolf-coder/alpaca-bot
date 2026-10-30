"""Verbindungsantworten, Authentifizierungsfehler und strikte Paper-Zieladressen."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.broker.alpaca import (
    AlpacaPaperBroker,
    EnvAuth,
    MissingAuth,
    ProxyAuth,
    build_url,
    validate_url,
)
from app.broker.base import BrokerError, OrderRequest
from app.connection_check import check_connection


async def test_connection_success_returns_only_summary(paper_broker, fake):
    res = await check_connection(paper_broker, "env")
    assert res["ok"] is True
    assert res["mode"] == "PAPER"
    assert res["host"] == "paper-api.alpaca.markets"
    assert {k for k in res} == {"ok", "mode", "host", "auth_mode", "checked_at", "account_status", "currency", "cash", "equity"}
    assert res["account_status"] == "ACTIVE" and res["currency"] == "USD"
    assert res["cash"] == 1000.0 and res["equity"] == 1000.0
    assert "PA-GEHEIM-123" not in str(res)  # keine Kontonummer
    assert "testsecretvalue" not in str(res)


async def test_env_mode_sends_apca_headers_without_bearer(paper_broker, fake):
    await paper_broker.get_account()
    _, _, headers = fake.calls[-1]
    assert headers["apca-api-key-id"] == "PKTESTKEYID"
    assert headers["apca-api-secret-key"] == "testsecretvalue"
    assert "authorization" not in headers
    assert not headers["apca-api-key-id"].lower().startswith("bearer")


async def test_proxy_mode_sends_no_credentials(proxy_broker, fake):
    await proxy_broker.get_account()
    _, _, headers = fake.calls[-1]
    assert "apca-api-key-id" not in headers
    assert "apca-api-secret-key" not in headers
    assert "authorization" not in headers


async def test_proxy_mode_without_proxy_is_config_error(fake, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    b = AlpacaPaperBroker(ProxyAuth(), transport=httpx.MockTransport(fake.handler))
    res = await check_connection(b, "proxy")
    assert res["ok"] is False and res["error"]["kind"] == "config_missing"
    assert fake.calls == []  # kein Aufruf ohne Konfiguration


async def test_env_mode_missing_keys_reported_by_name(fake, monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "x")
    b = AlpacaPaperBroker(EnvAuth(), transport=httpx.MockTransport(fake.handler))
    res = await check_connection(b, "env")
    assert res["error"]["kind"] == "config_missing"
    assert "ALPACA_API_KEY" in res["error"]["message"]
    assert "ALPACA_SECRET_KEY" not in res["error"]["message"]


async def test_auth_mode_unset_is_config_error(fake):
    b = AlpacaPaperBroker(MissingAuth(), transport=httpx.MockTransport(fake.handler))
    res = await check_connection(b, None)
    assert res["error"]["kind"] == "config_missing"


@pytest.mark.parametrize(
    "response,kind",
    [
        (httpx.Response(401, json={"code": 40110000, "message": "request is not authorized"}), "auth_rejected"),
        (httpx.Response(403, json={"code": 40310000, "message": "forbidden"}), "forbidden"),
        (httpx.Response(422, json={"message": "invalid"}), "unprocessable"),
        (httpx.Response(404, json={"message": "nope"}), "not_found"),
        (httpx.Response(418, text="teapot"), "broker_error"),
    ],
)
async def test_client_errors_classified_without_retry(paper_broker, fake, response, kind):
    fake.inject[("GET", "/v2/account")] = [response]
    res = await check_connection(paper_broker, "env")
    assert res["ok"] is False and res["error"]["kind"] == kind
    assert fake.count("GET", "/v2/account") == 1


async def test_auth_hint_differs_for_proxy(proxy_broker, fake):
    fake.inject[("GET", "/v2/account")] = [httpx.Response(401, json={"message": "unauthorized"})]
    res = await check_connection(proxy_broker, "proxy")
    assert "Network Secret" in res["error"]["hint"]


async def test_timeout_is_retried_then_reported(paper_broker, fake):
    fake.inject[("GET", "/v2/account")] = [httpx.ReadTimeout("t")] * 3
    res = await check_connection(paper_broker, "env")
    assert res["error"]["kind"] == "timeout"
    assert fake.count("GET", "/v2/account") == 3


async def test_transient_errors_recover_with_backoff(paper_broker, fake):
    fake.inject[("GET", "/v2/account")] = [httpx.ConnectError("x"), httpx.Response(503, json={"message": "busy"})]
    res = await check_connection(paper_broker, "env")
    assert res["ok"] is True
    assert fake.count("GET", "/v2/account") == 3


async def test_rate_limit_reported(paper_broker, fake):
    fake.inject[("GET", "/v2/account")] = [httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "too many"})] * 3
    res = await check_connection(paper_broker, "env")
    assert res["error"]["kind"] == "rate_limited"


async def test_proxy_denial_not_retried(paper_broker, fake):
    fake.inject[("GET", "/v2/account")] = [httpx.ProxyError("403 Forbidden")] * 3
    res = await check_connection(paper_broker, "env")
    assert res["error"]["kind"] == "network"
    assert fake.count("GET", "/v2/account") == 1


async def test_redirect_not_followed(paper_broker, fake):
    fake.inject[("GET", "/v2/account")] = [httpx.Response(302, headers={"Location": "https://evil.example/v2/account"})]
    res = await check_connection(paper_broker, "env")
    assert res["error"]["kind"] == "unexpected_redirect"
    assert all(c[0] != "GET" or "evil" not in c[1] for c in fake.calls)
    assert fake.count("GET", "/v2/account") == 1


async def test_post_order_never_retried_on_timeout(paper_broker, fake):
    fake.inject[("POST", "/v2/orders")] = [httpx.ReadTimeout("lost")]
    with pytest.raises(BrokerError) as ei:
        await paper_broker.submit_order(
            OrderRequest(symbol="SPY", side="buy", notional=Decimal("5"), client_order_id="cid-1")
        )
    assert ei.value.kind == "timeout" and ei.value.may_have_been_sent is True
    assert fake.count("POST", "/v2/orders") == 1


async def test_post_order_5xx_not_retried(paper_broker, fake):
    fake.inject[("POST", "/v2/orders")] = [httpx.Response(500, json={"message": "boom"})]
    with pytest.raises(BrokerError) as ei:
        await paper_broker.submit_order(OrderRequest(symbol="SPY", side="buy", qty=Decimal("1"), client_order_id="c2"))
    assert ei.value.may_have_been_sent is True
    assert fake.count("POST", "/v2/orders") == 1


async def test_connect_error_on_post_means_not_sent(paper_broker, fake):
    fake.inject[("POST", "/v2/orders")] = [httpx.ConnectError("refused")]
    with pytest.raises(BrokerError) as ei:
        await paper_broker.submit_order(OrderRequest(symbol="SPY", side="buy", qty=Decimal("1"), client_order_id="c3"))
    assert ei.value.may_have_been_sent is False


# ---------------------------------------------------------------- URLs
def test_build_url_only_fixed_hosts():
    assert str(build_url("trading", "/v2/account")) == "https://paper-api.alpaca.markets/v2/account"
    assert str(build_url("data", "/v2/stocks/SPY/bars")) == "https://data.alpaca.markets/v2/stocks/SPY/bars"


@pytest.mark.parametrize("path", ["/v1/account", "/v2/../x", "//evil.com/v2", "/v2/a?x=1", "/v2/a@b", "v2/account"])
def test_build_url_rejects_bad_paths(path):
    with pytest.raises(BrokerError) as ei:
        build_url("trading", path)
    assert ei.value.kind == "invalid_url"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets/v2/account",  # Live-Host – nie erlaubt
        "http://paper-api.alpaca.markets/v2/account",
        "https://paper-api.alpaca.markets:8443/v2/account",
        "https://user:pw@paper-api.alpaca.markets/v2/account",
        "https://paper-api.alpaca.markets.evil.com/v2/account",
        "https://data.alpaca.markets/v2/account",  # Datenhost für Handel
    ],
)
def test_validate_url_rejects(url):
    with pytest.raises(BrokerError):
        validate_url(httpx.URL(url), "trading")


def test_no_live_url_anywhere_in_code():
    from pathlib import Path

    src = "".join(p.read_text() for p in Path(__file__).resolve().parents[1].joinpath("app").rglob("*.py"))
    assert "https://api.alpaca.markets" not in src


async def test_request_guard_blocks_foreign_host(paper_broker):
    with pytest.raises(BrokerError) as ei:
        await paper_broker._client.get("https://example.com/v2/account")
    assert ei.value.kind == "invalid_url"


async def test_invalid_symbol_never_requested(paper_broker, fake):
    with pytest.raises(BrokerError):
        await paper_broker.get_asset("../account")
    assert fake.calls == []
