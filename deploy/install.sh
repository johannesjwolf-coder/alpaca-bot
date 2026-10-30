#!/usr/bin/env bash
# Einrichtung des Alpaca Paper-Bots auf einem frischen Ubuntu-24.04-Server.
#
#   git clone -b claude/pensive-sagan-wjwgcp https://github.com/johannesjwolf-coder/alpaca-bot /opt/alpaca-bot
#   sudo bash /opt/alpaca-bot/deploy/install.sh
#
# Das Skript kann gefahrlos mehrfach laufen. Es
#   1. installiert Docker, Git, jq, sqlite3, Firewall und automatische Sicherheitsupdates,
#   2. sperrt alle eingehenden Verbindungen ausser SSH und Tailscale,
#   3. installiert Tailscale und meldet den Server in Ihrem Tailnet an,
#   4. fragt die Alpaca-PAPER-Schlüssel verdeckt ab (nur beim ersten Mal) und legt die .env an,
#   5. erzeugt ein ntfy-Thema für Push-Nachrichten,
#   6. baut und startet die Anwendung (nur auf 127.0.0.1 des Servers),
#   7. macht sie über "tailscale serve" per HTTPS nur in Ihrem Tailnet erreichbar,
#   8. richtet ein tägliches Datenbank-Backup ein.
set -euo pipefail
umask 077

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy/common.sh
source "$APP_DIR/deploy/common.sh"
TS_HOSTNAME="${TS_HOSTNAME:-alpaca-bot}"

require_root
grep -q 'VERSION_ID="24.04"' /etc/os-release 2>/dev/null || warn "Getestet für Ubuntu 24.04 – andere Versionen auf eigenes Risiko."

# ------------------------------------------------------------------ Pakete
info "Pakete installieren (Docker, Git, jq, sqlite3, Firewall, automatische Updates) …"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git jq sqlite3 openssl ufw unattended-upgrades docker.io docker-compose-v2 >/dev/null
systemctl enable --now docker >/dev/null
ok "Pakete installiert ($(docker compose version --short 2>/dev/null || echo 'compose ?'))"

cat >/etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
CONF
ok "Automatische Sicherheitsupdates aktiv (ohne automatischen Neustart)"

# ------------------------------------------------------------------ Firewall
info "Firewall: eingehend nur SSH und Tailscale …"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow in on tailscale0 >/dev/null
ufw allow 41641/udp >/dev/null # Tailscale-Direktverbindungen (sonst über Relais, langsamer)
ufw --force enable >/dev/null
ok "Firewall aktiv"

# ------------------------------------------------------------------ Tailscale
if ! command -v tailscale >/dev/null 2>&1; then
  info "Tailscale installieren (offizielles Paketarchiv) …"
  curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list
  chmod 644 /usr/share/keyrings/tailscale-archive-keyring.gpg /etc/apt/sources.list.d/tailscale.list
  apt-get update -qq
  apt-get install -y -qq tailscale >/dev/null
fi
systemctl enable --now tailscaled >/dev/null
if [[ -z "$(ts_dns_name)" ]] || ! tailscale status >/dev/null 2>&1; then
  info "Server im Tailnet anmelden – öffnen Sie gleich den angezeigten Link und bestätigen Sie das Gerät."
  tailscale up --hostname="$TS_HOSTNAME"
fi
TS_NAME="$(ts_dns_name)"
[[ -n "$TS_NAME" ]] || die "Tailscale-Name nicht ermittelbar. Ist MagicDNS im Tailscale-Adminbereich (DNS) aktiviert?"
ok "Tailscale verbunden: $TS_NAME"

# ------------------------------------------------------------------ .env
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$APP_DIR/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"
set_env ALPACA_AUTH_MODE env

if [[ -z "$(get_env ALPACA_API_KEY)" || -z "$(get_env ALPACA_SECRET_KEY)" ]]; then
  echo
  info "Alpaca-PAPER-Schlüssel eingeben (app.alpaca.markets → Paper-Konto → API Keys)."
  echo "   Die Eingabe bleibt unsichtbar und wird nur in $ENV_FILE (nur für root lesbar) gespeichert."
  read -rsp "   API Key ID: " key_id
  echo
  read -rsp "   Secret Key: " secret_key
  echo
  [[ -n "$key_id" && -n "$secret_key" ]] || die "Leere Eingabe – nichts gespeichert. Skript bitte erneut starten."
  if [[ "$key_id" != PK* ]]; then
    warn "Paper-Schlüssel beginnen normalerweise mit „PK“. Live-Schlüssel werden nicht unterstützt."
    read -rp "   Trotzdem speichern? [j/N] " answer
    [[ "$answer" =~ ^[jJyY]$ ]] || die "Abgebrochen – nichts gespeichert."
  fi
  set_env ALPACA_API_KEY "$key_id"
  set_env ALPACA_SECRET_KEY "$secret_key"
  unset key_id secret_key
  ok "Schlüssel gespeichert"
else
  ok "Alpaca-Schlüssel bereits vorhanden (werden nicht angezeigt)"
fi

if [[ -z "$(get_env NTFY_TOPIC)" ]]; then
  set_env NTFY_TOPIC "alpaca-bot-$(openssl rand -hex 12)"
  NEW_TOPIC=1
fi
[[ -n "$(get_env APP_TIMEZONE)" ]] || set_env APP_TIMEZONE "Europe/Zurich"
set_env APP_ALLOWED_HOSTS "localhost,127.0.0.1,$TS_NAME"
set_env APP_ALLOWED_ORIGINS "https://$TS_NAME,http://127.0.0.1:8000,http://localhost:8000"
ok ".env vorbereitet ($ENV_FILE, Rechte 600)"

# ------------------------------------------------------------------ Start
info "Anwendung bauen und starten (dauert beim ersten Mal einige Minuten) …"
"${COMPOSE[@]}" up -d --build --force-recreate # übernimmt geänderte .env-Werte sicher
health || die "Anwendung antwortet nicht. Protokoll: docker compose -f $APP_DIR/docker-compose.yml logs --tail=100"
ok "Anwendung läuft auf 127.0.0.1:8000 (vom Internet nicht erreichbar)"

info "Verbindungstest zu Alpaca (nur lesend) …"
if "${COMPOSE[@]}" exec -T alpaca-bot python -m app.connection_check; then
  ok "Alpaca-Paper-Konto erreichbar"
else
  warn "Verbindungstest fehlgeschlagen – Schlüssel prüfen: sudo bash $APP_DIR/deploy/set-keys.sh"
fi

info "HTTPS im Tailnet einrichten (tailscale serve) …"
# Falls HTTPS-Zertifikate im Tailnet noch aus sind, zeigt Tailscale hier einen Link zum Einschalten.
tailscale serve --bg --https=443 http://127.0.0.1:8000
# Prüft, ob die App die Tailscale-Adresse annimmt (sonst „Invalid host header“ im Browser).
code="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $TS_NAME" http://127.0.0.1:8000/api/status || true)"
[[ "$code" == 200 ]] || die "Die App lehnt die Adresse $TS_NAME ab (HTTP $code). Bitte melden."
ok "Erreichbar unter https://$TS_NAME (nur Geräte in Ihrem Tailnet)"

# ------------------------------------------------------------------ Backup
cat >/etc/cron.d/alpaca-bot-backup <<CRON
# Tägliches Backup der Bot-Datenbank (keine Zugangsdaten), 14 Tage aufbewahrt
SHELL=/bin/bash
17 3 * * * root $APP_DIR/deploy/backup.sh >>/var/log/alpaca-bot-backup.log 2>&1
CRON
chmod 644 /etc/cron.d/alpaca-bot-backup
ok "Tägliches Backup um 03:17 nach $BACKUP_DIR"

echo
ok "Fertig."
echo "   Oberfläche:   https://$TS_NAME   (Tailscale auf Handy/PC einschalten)"
if [[ "${NEW_TOPIC:-0}" == 1 ]]; then
  echo "   ntfy-Thema:   $(get_env NTFY_TOPIC)"
  echo "                 In der ntfy-App abonnieren (Server ntfy.sh). Wie ein Passwort behandeln."
fi
echo "   Updates:      sudo bash $APP_DIR/deploy/update.sh"
echo "   Protokoll:    sudo docker compose -f $APP_DIR/docker-compose.yml logs -f --tail=100"
echo "   Die Anwendung startet im DEMO-Modus; Automatik und Paper-Orderfreigabe sind aus."
