"""Lesender Verbindungstest gegen das Alpaca-Paper-Konto (GET /v2/account).

Ausführen (im Ordner backend):
    uv run python -m app.connection_check

Ausgegeben werden nur Modus, Kontostatus, Währung, Cash und gesamter Kontowert –
keine Kontonummer, keine Zugangsdaten.
Exit-Codes: 0 Erfolg, 2 Konfiguration fehlt, 3 Authentifizierung/Berechtigung,
4 Netzwerk/Timeout, 5 Rate Limit, 6 sonstiger Brokerfehler.
"""

from __future__ import annotations

import asyncio
import sys

from .broker.alpaca import AlpacaPaperBroker
from .broker.base import Broker, BrokerError
from .config import PAPER_TRADING_HOST, load_settings
from .timeutil import utcnow

EXIT_CODES = {
    "config_missing": 2,
    "auth_rejected": 3,
    "forbidden": 3,
    "network": 4,
    "timeout": 4,
    "rate_limited": 5,
}

KIND_LABELS = {
    "config_missing": "Fehlende Konfiguration",
    "auth_rejected": "Authentifizierung abgelehnt",
    "forbidden": "Unzureichende Berechtigungen",
    "network": "Netzwerkfehler",
    "timeout": "Zeitüberschreitung",
    "rate_limited": "Rate Limit erreicht",
    "broker_error": "Brokerfehler",
    "unexpected_redirect": "Unerwartete Weiterleitung",
    "invalid_response": "Ungültige Antwort",
    "invalid_url": "Unzulässige Zieladresse",
    "not_found": "Nicht gefunden",
    "unprocessable": "Anfrage abgelehnt",
}


async def check_connection(broker: Broker, auth_mode: str | None) -> dict:
    checked_at = utcnow().isoformat()
    try:
        acct = await broker.get_account()
    except BrokerError as exc:
        return {
            "ok": False,
            "mode": "PAPER",
            "host": PAPER_TRADING_HOST,
            "auth_mode": auth_mode,
            "checked_at": checked_at,
            "error": {**exc.to_dict(), "label": KIND_LABELS.get(exc.kind, exc.kind)},
        }
    return {
        "ok": True,
        "mode": "PAPER",
        "host": PAPER_TRADING_HOST,
        "auth_mode": auth_mode,
        "checked_at": checked_at,
        "account_status": acct.status,
        "currency": acct.currency,
        "cash": acct.cash,
        "equity": acct.equity,
    }


async def _main() -> int:
    load_settings()  # lädt optional .env (nur Prozessumgebung, keine Ausgabe)
    broker = AlpacaPaperBroker()
    try:
        res = await check_connection(broker, broker.auth.mode)
    finally:
        await broker.aclose()
    print(f"Modus:        PAPER ({res['host']})")
    print(f"Auth-Modus:   {res['auth_mode']}")
    print(f"Geprüft:      {res['checked_at']}")
    if res["ok"]:
        print("Ergebnis:     Verbindung erfolgreich (GET /v2/account)")
        print(f"Kontostatus:  {res['account_status']}")
        print(f"Währung:      {res['currency']}")
        print(f"Cash:         {res['cash']:,.2f} {res['currency']}")
        print(f"Kontowert:    {res['equity']:,.2f} {res['currency']}")
        return 0
    err = res["error"]
    print(f"Ergebnis:     FEHLGESCHLAGEN – {err['label']}")
    print(f"Meldung:      {err['message']}")
    if err.get("hint"):
        print(f"Nächster Schritt: {err['hint']}")
    return EXIT_CODES.get(err["kind"], 6)


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
