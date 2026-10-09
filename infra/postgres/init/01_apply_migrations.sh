#!/usr/bin/env bash
# Apply migration files in numeric order on first container init.
# Subsequent migrations (running container) are applied by scripts/migrate.sh.
set -euo pipefail

MIGRATIONS_DIR=/migrations/versions
[ -d "$MIGRATIONS_DIR" ] || { echo "no migrations dir; skipping"; exit 0; }

for f in $(ls "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort); do
  echo "[woxpas-init] applying $(basename "$f")"
  psql --set=ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$f"
done

echo "[woxpas-init] all migrations applied"
