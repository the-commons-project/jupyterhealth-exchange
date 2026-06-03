#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [ ! -f ".env" ]; then
  printf "\nCannot find .env\n"
  exit 2
fi
set -a; source .env; set +a
PGPASSWORD="$DB_PASSWORD" psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" "$DB_NAME"
