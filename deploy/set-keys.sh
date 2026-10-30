#!/usr/bin/env bash
# Alpaca-PAPER-Schlüssel auf dem Server ersetzen (z. B. nach dem Neu-Erzeugen bei Alpaca).
# Eingabe verdeckt; die Werte werden nur in /opt/alpaca-bot/.env gespeichert und nie angezeigt.
set -euo pipefail
umask 077
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy/common.sh
source "$APP_DIR/deploy/common.sh"
require_root
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE fehlt – zuerst deploy/install.sh ausführen."
if [[ -z "$(get_env APP_ALLOWED_HOSTS)" ]]; then
  warn "Die Installation ist nicht abgeschlossen (Adresse, Push-Nachrichten und Backup fehlen)."
  warn "Bitte stattdessen ausführen: bash $APP_DIR/deploy/install.sh  – es fragt die Schlüssel mit ab."
  exit 1
fi

read -rsp "API Key ID (Paper): " key_id
echo
read -rsp "Secret Key (Paper): " secret_key
echo
[[ -n "$key_id" && -n "$secret_key" ]] || die "Leere Eingabe – nichts geändert."
if [[ "$key_id" != PK* ]]; then
  warn "Paper-Schlüssel beginnen normalerweise mit „PK“."
  read -rp "Trotzdem speichern? [j/N] " answer
  [[ "$answer" =~ ^[jJyY]$ ]] || die "Abgebrochen – nichts geändert."
fi
set_env ALPACA_API_KEY "$key_id"
set_env ALPACA_SECRET_KEY "$secret_key"
unset key_id secret_key
ok "Gespeichert. Anwendung wird neu gestartet (Automatik danach aus, ausser „Fortsetzen nach Neustart“ ist aktiv) …"
"${COMPOSE[@]}" up -d --force-recreate
health || die "Anwendung antwortet nicht."
"${COMPOSE[@]}" exec -T alpaca-bot python -m app.connection_check && ok "Verbindung erfolgreich"
