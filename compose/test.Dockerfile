# Test-runner image for docker-compose.test.yml: the app image plus uv and the dev
# dependency group (pytest, pytest-django, coverage...). Dev-only.
#
# Built on jhe-dev:local, the image docker-compose.yml builds from ../Dockerfile, so the
# Python version, system packages and runtime dependencies are inherited rather than
# repeated here. The code baked into that image is ignored: tests run against the repo,
# bind-mounted at /code.
#
# scripts/dev/test.sh and check.sh rebuild this image (and its base) only when
# ../Dockerfile, this file, pyproject.toml or uv.lock change, recorded in the label below.
ARG BASE_IMAGE=jhe-dev:local
FROM ${BASE_IMAGE}

# uv to install the dev group the same way ../Dockerfile installs the rest (into the system
# interpreter); uvx so check.sh can run ruff at pre-commit's version.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

# pyproject.toml and uv.lock are already in the base image at /code.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --only-group dev --no-emit-project --format requirements.txt -o /tmp/dev-requirements.txt \
 && uv pip install --system --no-deps -r /tmp/dev-requirements.txt

# Last, so a fingerprint change alone doesn't invalidate the install layer above.
ARG DEPS_FINGERPRINT=unknown
LABEL org.jupyterhealth.jhe.test-deps-fingerprint=${DEPS_FINGERPRINT}
