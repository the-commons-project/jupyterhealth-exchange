#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [ ! -f ".env" ]; then
  printf "\nCannot find .env\n"
  exit 2
fi
set -a; source .env; set +a
PGPASSWORD="$DB_PASSWORD" pg_dump -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -d "$DB_NAME" -F plain --column-inserts --data-only > dump.txt
