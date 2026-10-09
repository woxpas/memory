#!/usr/bin/env bash
# Apply pending migrations to a running Postgres container.
# Idempotent: skips versions already in schema_migrations.
set -euo pipefail

: "${POSTGRES_DB:?must be set}"
: "${POSTGRES_USER:?must be set}"
: "${POSTGRES_PASSWORD:?must be set}"
HOST="${POSTGRES_HOST:-localhost}"
PORT="${POSTGRES_PORT:-5432}"
SERVICE="${WOXPAS_PG_SERVICE:-postgres}"

PSQL=(docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" "$SERVICE" \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -h "$HOST" -p "$PORT")

applied=$("${PSQL[@]}" -At -c "SELECT version FROM schema_migrations" 2>/dev/null || true)

cd "$(dirname "$0")/.."

for f in migrations/versions/*.sql; do
  base=$(basename "$f" .sql)
  if echo "$applied" | grep -qx "$base"; then
    continue
  fi
  echo "[migrate] applying $base"
  "${PSQL[@]}" < "$f"
done

echo "[migrate] up to date"
