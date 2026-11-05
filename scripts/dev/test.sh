#!/usr/bin/env bash
# Run the backend test suite in a container (docker-compose.test.yml), against a
# disposable, RAM-backed Postgres. Needs only Docker; your dev stack and its data are
# never touched.
#
#   scripts/dev/test.sh                                  all of tests/backend/
#   scripts/dev/test.sh tests/backend/test_study_viewset.py -k scope
#   scripts/dev/test.sh --fresh                          recreate the test DB first
#
# The test DB stays up between runs and pytest reuses the database, so warm runs skip
# migrations. Use --fresh after switching branches or changing migrations. Any other
# arguments go straight to pytest.
set -euo pipefail
cd "$(dirname "$0")/../.."

fresh=0
has_path=0
args=()
for arg in "$@"; do
    case "$arg" in
        --fresh) fresh=1 ;;
        *)
            args+=("$arg")
            # A file/dir (optionally with ::test_name) means the caller chose what to run.
            [ -e "${arg%%::*}" ] && has_path=1
            ;;
    esac
done
# Default to the suite CI runs. A bare `pytest` from the repo root also collects
# mcp_server/ and tests/contract/, which need their own environments.
[ "$has_path" = 1 ] || args+=(tests/backend/)

# shellcheck source=scripts/dev/lib.sh
source scripts/dev/lib.sh

compose=(docker compose -f docker-compose.test.yml)
if [ "$fresh" = 1 ]; then
    "${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
fi
ensure_test_image
"${compose[@]}" up -d --wait test-db >/dev/null

db_args=(--reuse-db)
[ "$fresh" = 1 ] && db_args+=(--create-db)

# No TTY when output is piped (scripts, logs).
if [ -t 0 ] && [ -t 1 ]; then tty=(); else tty=(-T); fi

exec "${compose[@]}" run --rm ${tty[@]+"${tty[@]}"} test-runner \
    python -m pytest "${db_args[@]}" "${args[@]}"
