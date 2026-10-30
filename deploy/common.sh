# Gemeinsame Hilfen für die Server-Skripte (wird per "source" geladen).
# Bewusst ohne "set -x": Werte aus der .env dürfen nie im Terminal oder in Logs landen.

APP_DIR="${APP_DIR:-/opt/alpaca-bot}"
ENV_FILE="$APP_DIR/.env"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/alpaca-bot}"
COMPOSE=(docker compose --project-directory "$APP_DIR" -f "$APP_DIR/docker-compose.yml")

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m✔\033[0m  %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m  %s\n' "$*" >&2; }
die() { printf '\033[1;31m✘\033[0m  %s\n' "$*" >&2; exit 1; }

require_root() { [[ $EUID -eq 0 ]] || die "Bitte mit sudo bzw. als root ausführen."; }

# Liest einen Wert aus der .env (reines Bash, ohne externe Programme).
get_env() {
  local key="$1" line
  [[ -f "$ENV_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$key="* ]]; then
      printf '%s' "${line#*=}"
      return 0
    fi
  done <"$ENV_FILE"
}

# Setzt KEY=VALUE in der .env. Der Wert wird nur mit Bash-Builtins geschrieben
# und erscheint daher weder in Prozesslisten noch im Terminal.
set_env() {
  local key="$1" value="$2" line found=0 tmp
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  chmod 600 "$tmp"
  if [[ -f "$ENV_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "$key="* ]]; then
        printf '%s=%s\n' "$key" "$value" >>"$tmp"
        found=1
      else
        printf '%s\n' "$line" >>"$tmp"
      fi
    done <"$ENV_FILE"
  fi
  [[ $found -eq 1 ]] || printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$ENV_FILE"
}

ts_dns_name() {
  command -v tailscale >/dev/null 2>&1 || return 0
  tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//'
}

health() {
  local i
  for i in $(seq 1 30); do
    if curl -fsS --max-time 3 http://127.0.0.1:8000/api/status >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

bot_running() {
  curl -fsS --max-time 3 http://127.0.0.1:8000/api/status 2>/dev/null | jq -e '.bot.running == true' >/dev/null 2>&1
}

resume_enabled() {
  curl -fsS --max-time 3 http://127.0.0.1:8000/api/status 2>/dev/null | jq -e '.resume_after_restart == true' >/dev/null 2>&1
}
