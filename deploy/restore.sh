#!/usr/bin/env bash
# Datenbank aus einem Backup zurückspielen.  Aufruf: sudo bash restore.sh /var/backups/alpaca-bot/<datei>.sqlite3.gz
set -euo pipefail
umask 077
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy/common.sh
source "$APP_DIR/deploy/common.sh"
require_root
FILE="${1:-}"
[[ -f "$FILE" ]] || die "Backup-Datei angeben, z. B.: sudo bash restore.sh $BACKUP_DIR/alpaca-bot-JJJJMMTT-hhmmss.sqlite3.gz"

VOLUME="$(docker volume ls -q | grep -E '(^|_)alpaca-bot-data$' | head -1 || true)"
[[ -n "$VOLUME" ]] || die "Docker-Volume alpaca-bot-data nicht gefunden."
DIR="$(docker volume inspect -f '{{ .Mountpoint }}' "$VOLUME")"

warn "Die Anwendung wird gestoppt und die aktuelle Datenbank ersetzt (vorher wird sie gesichert)."
read -rp "Fortfahren? [j/N] " answer
[[ "$answer" =~ ^[jJyY]$ ]] || die "Abgebrochen."
"$APP_DIR/deploy/backup.sh" || true
"${COMPOSE[@]}" stop
OWNER="$(stat -c '%u:%g' "$DIR/alpaca-bot.sqlite3" 2>/dev/null || echo 10001:10001)"
rm -f "$DIR/alpaca-bot.sqlite3" "$DIR/alpaca-bot.sqlite3-wal" "$DIR/alpaca-bot.sqlite3-shm"
if [[ "$FILE" == *.gz ]]; then gunzip -c "$FILE" >"$DIR/alpaca-bot.sqlite3"; else cp "$FILE" "$DIR/alpaca-bot.sqlite3"; fi
chown "$OWNER" "$DIR/alpaca-bot.sqlite3"
chmod 600 "$DIR/alpaca-bot.sqlite3"
"${COMPOSE[@]}" start
health && ok "Wiederhergestellt aus $FILE. Automatik und Orderfreigabe sind aus."
