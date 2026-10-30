#!/usr/bin/env bash
# Konsistentes Backup der SQLite-Datenbank aus dem Docker-Volume (auch bei laufender Anwendung).
# Enthält Orders, Trades, Einstellungen und Ereignisse – keine Zugangsdaten (die stehen nur in .env).
set -euo pipefail
umask 077
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy/common.sh
source "$APP_DIR/deploy/common.sh"
require_root
KEEP_DAYS="${KEEP_DAYS:-14}"

VOLUME="$(docker volume ls -q | grep -E '(^|_)alpaca-bot-data$' | head -1 || true)"
[[ -n "$VOLUME" ]] || die "Docker-Volume alpaca-bot-data nicht gefunden."
SRC="$(docker volume inspect -f '{{ .Mountpoint }}' "$VOLUME")/alpaca-bot.sqlite3"
[[ -f "$SRC" ]] || die "Datenbank $SRC nicht gefunden."

mkdir -p "$BACKUP_DIR"
DEST="$BACKUP_DIR/alpaca-bot-$(date +%Y%m%d-%H%M%S).sqlite3"
sqlite3 "$SRC" ".backup '$DEST'"
sqlite3 "$DEST" "PRAGMA integrity_check;" | grep -qx ok || die "Integritätsprüfung des Backups fehlgeschlagen."
gzip -9 "$DEST"
find "$BACKUP_DIR" -name 'alpaca-bot-*.sqlite3.gz' -mtime "+$KEEP_DAYS" -delete
ok "Backup: $DEST.gz"
