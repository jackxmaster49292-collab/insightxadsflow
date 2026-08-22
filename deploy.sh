#!/usr/bin/env bash
#
# One command to bring the stack up cleanly, in the right order, with the checks
# that catch the mistakes people actually make.
#
#   ./deploy.sh              start or update
#   ./deploy.sh --rebuild    force a full image rebuild (no cache)
#   ./deploy.sh --status     show what is running, then exit
#   ./deploy.sh --logs       follow the backend logs, then exit
#
# Safe to re-run. It never deletes data.

set -euo pipefail

cd "$(dirname "$0")"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'

say()  { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '\n  %s✗ %s%s\n\n' "$RED" "$*" "$OFF" >&2; exit 1; }

case "${1:-}" in
  --status) "${COMPOSE[@]}" ps; exit 0 ;;
  --logs)   "${COMPOSE[@]}" logs -f api worker listener scheduler adminbot; exit 0 ;;
esac

# ---------------------------------------------------------------------------
# 1. Configuration checks — fail here, with an answer, not three minutes in.
# ---------------------------------------------------------------------------
say "Checking configuration"

[[ -f .env ]] || die ".env is missing. Run: cp .env.example .env   then fill it in."

for required in ENCRYPTION_KEK APP_SECRET_KEY; do
  value=$(grep -E "^${required}=" .env | cut -d= -f2- || true)
  [[ -n "$value" ]] || die "$required is empty in .env"
done
ok "required secrets present"

# DATABASE_URL / REDIS_URL in .env are the single most common cause of a broken
# deployment: Compose overrides them, so they look harmless right up until
# something reads them and points at a host that does not exist in the network.
if grep -qE '^\s*(DATABASE_URL|REDIS_URL)=' .env; then
  warn "DATABASE_URL / REDIS_URL are set in .env"
  warn "Compose sets these for you. Leaving them can break the workers."
  warn "Comment them out unless you run the backend outside Docker."
fi

for required in ADMIN_BOT_TOKEN ADMIN_TELEGRAM_IDS; do
  value=$(grep -E "^${required}=" .env | cut -d= -f2- || true)
  [[ -n "$value" ]] || die "$required is empty in .env — the control panel cannot start without it.
    ADMIN_BOT_TOKEN     from @BotFather (a separate bot from any forwarding bot)
    ADMIN_TELEGRAM_IDS  your numeric Telegram id, from @userinfobot"
done
ok "control panel is configured"

"${COMPOSE[@]}" config -q || die "compose files are invalid (see the error above)"
ok "compose configuration is valid"

# ---------------------------------------------------------------------------
# 2. Build and start
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--rebuild" ]]; then
  say "Rebuilding images from scratch"
  "${COMPOSE[@]}" build --no-cache
else
  say "Building images"
  "${COMPOSE[@]}" build
fi

# Data services first, and wait for them to be genuinely healthy. Starting
# everything at once is what produces "host 'postgres' does not resolve": the
# workers come up before the database container exists.
# An earlier version of this project used Compose's implicit default network.
# If that one is still around, containers can end up split across two bridges —
# and a container on the old one cannot resolve "postgres" on the new one.
if docker network inspect insightadflow_default >/dev/null 2>&1; then
  warn "removing stale network insightadflow_default from an older layout"
  docker network rm insightadflow_default >/dev/null 2>&1 || true
fi

# The project was renamed from insight-store to insightadflow. Compose scopes
# both container names and volumes to the project, so the old stack still holds
# the ports and the old data volume is invisible to this one. Neither failure
# explains itself: the first looks like "port already allocated", the second
# like an empty database.
if docker ps -aq --filter "label=com.docker.compose.project=insight-store" | grep -q .; then
  warn "containers from the old 'insight-store' project are still present"
  warn "they hold the same ports, so this deployment cannot start until they go:"
  warn "  docker compose -p insight-store down --remove-orphans"
  die "Remove them, then run ./deploy.sh again."
fi

if docker volume inspect insight-store_pgdata >/dev/null 2>&1 \
   && ! docker volume inspect insightadflow_pgdata >/dev/null 2>&1; then
  warn "found a database volume from the old project name: insight-store_pgdata"
  warn "this deployment will create a NEW, EMPTY database (insightadflow_pgdata)."
  warn "Nothing is deleted — the old volume stays untouched — but your existing"
  warn "connections and rules will not appear until the data is migrated."
  warn "See docs/DEPLOY_VPS.md, 'Upgrading from a version that had a web panel'."
fi

say "Starting postgres and redis"
"${COMPOSE[@]}" up -d postgres redis

printf '  waiting for postgres'
for _ in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T postgres pg_isready -U insight >/dev/null 2>&1; then
    printf '\n'; ok "postgres is accepting connections"; break
  fi
  printf '.'; sleep 2
done
"${COMPOSE[@]}" exec -T postgres pg_isready -U insight >/dev/null 2>&1 \
  || die "postgres never became ready. Check: ${COMPOSE[*]} logs postgres"

# ---------------------------------------------------------------------------
# 3. Schema — before the workers, which refuse to start without it.
# ---------------------------------------------------------------------------
# Check connectivity first: a bad DSN or a password mismatch surfaces here as a
# plain-language diagnosis rather than as an alembic stack trace.
say "Checking database connectivity"
if ! "${COMPOSE[@]}" run --rm --no-deps api python -m app.preflight --no-schema; then
  die "The database is not usable yet — see the explanation above."
fi

say "Applying database migrations"
"${COMPOSE[@]}" run --rm --no-deps api alembic upgrade head \
  || die "migrations failed. Check: ${COMPOSE[*]} logs postgres"
ok "schema is up to date"

# ---------------------------------------------------------------------------
# 4. Everything else
# ---------------------------------------------------------------------------
say "Starting the rest of the stack"
"${COMPOSE[@]}" up -d --remove-orphans

sleep 10

# ---------------------------------------------------------------------------
# 5. Report honestly
# ---------------------------------------------------------------------------
say "Status"
"${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Status}}'

# A crash-looping container spends most of its time reporting "Up", so status
# alone gives false confidence. Restart count is the honest signal.
failed=""
for svc in $("${COMPOSE[@]}" config --services); do
  cid=$("${COMPOSE[@]}" ps -q "$svc" 2>/dev/null || true)
  [[ -n "$cid" ]] || { failed+="$svc "; continue; }
  state=$(docker inspect -f '{{.State.Status}}:{{.RestartCount}}' "$cid" 2>/dev/null || echo "unknown:0")
  status="${state%%:*}"; restarts="${state##*:}"
  if [[ "$status" != "running" ]] || (( restarts > 0 )); then
    failed+="$svc "
  fi
done
failed=$(echo "$failed" | xargs || true)

echo
if [[ -n "$failed" ]]; then
  printf '  %s✗ These services are not healthy: %s%s\n\n' "$RED" "$(echo "$failed" | tr '\n' ' ')" "$OFF"
  for svc in $failed; do
    printf '  %s--- %s ---%s\n' "$BOLD" "$svc" "$OFF"
    "${COMPOSE[@]}" logs --tail=20 "$svc" 2>&1 | sed 's/^/    /'
    echo
  done
  die "Fix the errors above, then run ./deploy.sh again."
fi

ok "all services are up"

# Nothing is published to the host in production, so the check runs inside the
# network — which is also the only place the API is meant to be reachable from.
say "Health check"
if "${COMPOSE[@]}" exec -T api curl -fsS --max-time 10 http://localhost:8000/api/v1/health 2>/dev/null; then
  echo
  ok "the backend is responding"
else
  echo
  warn "the health endpoint did not respond"
  warn "check: ${COMPOSE[*]} logs api"
fi

echo
say "Done. Send /start to your admin bot."
echo "  logs:    ./deploy.sh --logs"
echo "  status:  ./deploy.sh --status"
