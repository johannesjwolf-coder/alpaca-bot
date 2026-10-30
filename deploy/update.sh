#!/usr/bin/env bash
# Neueste Version holen, neu bauen und starten.
#   sudo bash /opt/alpaca-bot/deploy/update.sh [--yes] [--rebuild]
#   --yes      nicht nachfragen, auch wenn die Automatik läuft
#   --rebuild  auch ohne neue Version neu bauen (z. B. für neue Basis-Images)
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy/common.sh
source "$APP_DIR/deploy/common.sh"
require_root
YES=0
REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --rebuild) REBUILD=1 ;;
    *) die "Unbekannte Option: $arg" ;;
  esac
done

BRANCH="$(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD)"
info "Neueste Version von $BRANCH holen …"
git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
OLD="$(git -C "$APP_DIR" rev-parse --short HEAD)"
NEW="$(git -C "$APP_DIR" rev-parse --short "origin/$BRANCH")"
if [[ "$OLD" == "$NEW" && $REBUILD -eq 0 ]]; then
  ok "Bereits aktuell ($OLD). Neu bauen trotzdem: update.sh --rebuild"
  exit 0
fi
[[ "$OLD" == "$NEW" ]] || git -C "$APP_DIR" log --oneline "$OLD..$NEW" | head -20 || true

if bot_running && ! resume_enabled && [[ $YES -eq 0 ]]; then
  warn "Die Automatik läuft. Nach dem Update ist sie AUS (Fortsetzen nach Neustart ist nicht aktiv)."
  read -rp "Trotzdem aktualisieren? [j/N] " answer
  [[ "$answer" =~ ^[jJyY]$ ]] || die "Abgebrochen – nichts geändert."
fi

info "Datenbank vorher sichern …"
"$APP_DIR/deploy/backup.sh" || warn "Backup fehlgeschlagen – Update läuft trotzdem weiter."

git -C "$APP_DIR" merge --ff-only --quiet "origin/$BRANCH"
info "Neu bauen und starten ($OLD → $NEW) …"
"${COMPOSE[@]}" up -d --build
health || die "Anwendung antwortet nicht. Protokoll: docker compose -f $APP_DIR/docker-compose.yml logs --tail=100"
docker image prune -f >/dev/null 2>&1 || true
ok "Aktualisiert auf $NEW."
