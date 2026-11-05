# Shared helpers for scripts/dev/*.sh. Source it after cd-ing to the repo root.

# Build the test-runner image (docker-compose.test.yml) if it's missing or out of date.
#
# The image is jhe-dev:local plus the dev dependency group. It only needs rebuilding when
# the Dockerfiles or dependencies change: tests run against the bind-mounted repo, so code
# edits don't matter. Plain `docker compose build` can't tell the difference (jhe-dev:local
# bakes in the code, so every edit looks like a change), so we fingerprint the inputs that
# do matter and keep the fingerprint as a label on the image.
ensure_test_image() {
    local fingerprint current
    if command -v sha256sum >/dev/null 2>&1; then
        fingerprint=$(cat Dockerfile compose/test.Dockerfile pyproject.toml uv.lock | sha256sum | cut -c1-16)
    else
        fingerprint=$(cat Dockerfile compose/test.Dockerfile pyproject.toml uv.lock | shasum -a 256 | cut -c1-16)
    fi
    current=$(docker image inspect -f '{{ index .Config.Labels "org.jupyterhealth.jhe.test-deps-fingerprint" }}' \
        jhe-test-runner:local 2>/dev/null || true)
    [ "$current" = "$fingerprint" ] && return 0

    echo "Building the test image (Dockerfiles or dependencies changed)..."
    docker compose build --quiet jhe &&
        docker compose -f docker-compose.test.yml build --quiet \
            --build-arg "DEPS_FINGERPRINT=$fingerprint" test-runner
}
