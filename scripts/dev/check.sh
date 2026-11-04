#!/usr/bin/env bash
# Run every quick check in containers (docker-compose.test.yml), without changing any
# files. Needs only Docker. Exits non-zero if any check fails.
#
#   scripts/dev/check.sh           lint, format, migrations, backend tests, frontend tests
#   scripts/dev/check.sh --quick   skip the test suites
#
# To auto-fix lint and formatting instead, run the pre-commit hooks.
set -uo pipefail
cd "$(dirname "$0")/../.."

quick=0
[ "${1:-}" = "--quick" ] && quick=1

compose=(docker compose -f docker-compose.test.yml)
if [ -t 0 ] && [ -t 1 ]; then tty=(); else tty=(-T); fi
in_runner() { "${compose[@]}" run --rm ${tty[@]+"${tty[@]}"} test-runner "$@"; }
in_frontend() { "${compose[@]}" run --rm ${tty[@]+"${tty[@]}"} test-frontend "$@"; }

failed=()
run() {
    local name="$1"
    shift
    echo
    echo "==> $name"
    if "$@"; then
        echo "--- $name: ok"
    else
        echo "--- $name: FAILED"
        failed+=("$name")
    fi
}

# shellcheck source=scripts/dev/lib.sh
source scripts/dev/lib.sh
ensure_test_image || exit 1
"${compose[@]}" up -d --wait test-db >/dev/null || exit 1

# The ruff version pinned for pre-commit, so local checks match pre-commit.ci.
ruff_version=$(awk '/ruff-pre-commit/ {getline; if (match($0, /v[0-9]+\.[0-9]+\.[0-9]+/)) print substr($0, RSTART + 1, RLENGTH - 1)}' .pre-commit-config.yaml)
: "${ruff_version:?could not read the ruff version from .pre-commit-config.yaml}"

# Same file set as the pre-commit hooks: tracked Python and notebook files only (a bare
# `ruff format .` also formats code blocks in Markdown, which pre-commit doesn't), with
# pyproject's excludes still applied to the explicit file list.
ruff_on_python_files() {
    in_runner sh -c 'v="$1"; shift; git ls-files -z -- "*.py" "*.ipynb" ":!:data/schema/**" | xargs -0 uvx "ruff@$v" "$@" --force-exclude' \
        sh "$ruff_version" "$@"
}
run "ruff check" ruff_on_python_files check
run "ruff format" ruff_on_python_files format --check
run "missing migrations" in_runner python manage.py makemigrations --check --dry-run

if [ "$quick" = 0 ]; then
    run "backend tests" scripts/dev/test.sh
    run "frontend tests" in_frontend sh -c "npm ci --silent --no-audit --no-fund && npm test --silent"
fi

echo
if [ ${#failed[@]} -gt 0 ]; then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
echo "All checks passed."
