# Stage 1: Build patient-frontend React app
FROM node:22-slim AS frontend-builder
WORKDIR /build
COPY patient-frontend/package.json patient-frontend/package-lock.json ./
RUN npm ci
COPY patient-frontend/ ./
RUN npm run build

# Stage 2: Python application
FROM python:3.12-slim-trixie

RUN apt-get -y update \
 && apt-get -y install --no-install-recommends postgresql-client git \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /code
RUN mkdir -p /data

WORKDIR /code

COPY Pipfile Pipfile.lock /code/

# omh-shim: install from the-commons-project/omh-shim. Pinned to a
# specific commit SHA for reproducible builds. Bump when the shim updates.
RUN pip install "git+https://github.com/the-commons-project/omh-shim.git@832c1e84e247dfe01dfa0b8f70bea6637292ca29"

ARG XDG_CACHE_DIR=/tmp/cache
RUN --mount=type=cache,target=${XDG_CACHE_DIR} \
    export PIP_CACHE_DIR=$XDG_CACHE_DIR/pip \
 && export PIPENV_CACHE_DIR=$XDG_CACHE_DIR/pipenv \
 && pip install pipenv \
 && pipenv install --system \
 && pip uninstall -y pipenv

# supercronic for the cron sidecar (jhe_cron in docker-compose.yml).
# Baked in so container starts are fast and don't depend on GitHub.
ADD https://github.com/aptible/supercronic/releases/download/v0.2.29/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

COPY . /code

# Copy built patient-frontend from Stage 1 into static assets.
# Vite's outDir is ../core/static/patient-frontend relative to /build,
# which resolves to /core/static/patient-frontend in the builder stage.
COPY --from=frontend-builder /core/static/patient-frontend/ /code/core/static/patient-frontend/

RUN python manage.py collectstatic --no-input

EXPOSE 8000

CMD ["gunicorn", "--bind", ":8000", "--workers", "2", "jhe.wsgi"]
