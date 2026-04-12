# Open Wearables Integration

JHE integrates with [Open Wearables](https://github.com/the-momentum/open-wearables) to ingest wearable health data (heart rate, SpO2, sleep, activity) from providers like Oura Ring. OW handles OAuth with wearable vendors and data collection. JHE handles patient consent, data normalization (via omh-shim), and FHIR storage.

## Architecture

Two ingestion pipelines are available, selected by `OW_PIPELINE_MODE`:

| Mode | Data Source | How it works |
|------|-----------|--------------|
| `normalized` | OW REST API | JHE polls OW's `/timeseries`, `/summaries`, `/events/sleep` endpoints. OW returns pre-normalized data. |
| `raw` | MinIO S3 | OW writes raw vendor API responses to MinIO. JHE reads JSON payloads from S3 and normalizes them via omh-shim. |

Both modes share the same patient frontend, consent flow, OW connection management, and FHIR storage.

## Quick Start

### 1. Configure `.env`

Copy `dot_env_example.txt` and set the pipeline mode:

```bash
cp dot_env_example.txt .env
```

Key variables:

| Variable | Values | Description |
|----------|--------|-------------|
| `OW_PIPELINE_MODE` | `normalized` (default), `raw` | Which ingestion pipeline to use |
| `OW_BACKEND_PATH` | path | Relative path to the OW backend repo |
| `OW_FRONTEND_PATH` | path | Relative path to the OW frontend repo |

### 2. Start the stack

```bash
docker compose --profile ow up --build -d
```

This starts: JHE (app + DB), OW (app + worker + beat + DB + Redis + frontend), and MinIO.

### 3. Initialize

```bash
# JHE migrations and seed data
docker exec <jhe_container> python manage.py migrate
docker exec <jhe_container> python manage.py seed
```

### 4. Configure JHE Settings

The following `JheSetting` keys must be set (via System Settings UI or shell):

| Key | Example | Description |
|-----|---------|-------------|
| `ow.api_base_url` | `http://<ow_container>:8000` | OW backend URL (internal Docker network) |
| `ow.api_key` | `sk-...` | API key registered in OW |
| `ow.ingest_mode` | `polling` | `polling`, `webhook`, or `disabled` |
| `ow.lookback_days` | `7` | Sliding window for incremental polls |
| `ow.initial_backfill_days` | `30` | Window for first poll of a new patient |

For raw mode, also set:

| Key | Example | Description |
|-----|---------|-------------|
| `s3.endpoint_url` | `http://ow-object-store:9000` | MinIO endpoint |
| `s3.access_key_id` | `minioadmin` | MinIO access key |
| `s3.secret_access_key` | `minioadmin` | MinIO secret key |
| `s3.bucket_name` | `raw-payloads` | S3 bucket name |
| `s3.key_prefix` | `raw-payloads/oura/api_response` | Key prefix filter |

### 5. Set up OW

1. Log into OW admin at `http://localhost:<OW_FRONTEND_PORT>/`
   - Default: `admin@admin.com` / `your-secure-password`
2. Create an API key under Credentials
3. Set `ow.api_key` in JHE to match

### 6. Set up a study

In JHE, create:
- A **Study** with OW scope requests (e.g., `omh:heart-rate:2.0`)
- A **DataSource** with `type=personal_device` and `provider_key=oura`
- Link the DataSource to the Study via **StudyDataSource**
- Add **DataSourceSupportedScope** entries matching the study scopes
- Create an **OAuth Application** (`public`, `authorization-code`, `RS256`)
- Link it to the Study via **StudyClient**
- Set `client.code_verifier` and `client.invitation_url` JheSettings scoped to the app ID

### 7. Patient flow

1. Practitioner enrolls patient in study, generates invite link
2. Patient opens invite URL → consent page → selects scopes → connects Oura via OAuth
3. JHE creates an OW user, OW handles Oura OAuth
4. Cron (`ow_poll`) runs daily at 6am, ingests data for all connected patients

Manual poll: `docker exec <jhe_container> python manage.py ow_poll --patient-id <pk>`

## Sandbox Testing

For development without a real Oura account, use Oura's sandbox API.

### Prerequisites

- OW must have `OURA_USE_SANDBOX=true` in its env (set in `open-wearables/backend/config/.env`)
- Patient must have completed the invite + consent + Oura connect flow

### Run the sandbox test

```bash
./scripts/sandbox_test.sh <patient-email> [days]
```

The script:
1. Checks `OURA_USE_SANDBOX=true` (refuses to run otherwise)
2. Detects `OW_PIPELINE_MODE` automatically
3. **Normalized mode**: runs ordered sync script inside OW container (HR → SpO2 → sleep → rest)
4. **Raw mode**: triggers OW API sync (which writes raw payloads to MinIO)
5. Runs `ow_poll` to ingest into JHE
6. Reports observation counts by type

### Switching modes

```bash
# Edit .env
OW_PIPELINE_MODE=raw   # or normalized

# Recreate JHE container (env var is read at startup)
docker compose up -d jhe
```

## Code Structure

```
core/services/ow_ingest/
    __init__.py                    # Dispatcher — routes by OW_PIPELINE_MODE
    _common.py                     # Shared helpers (dedup, FHIR wrapping, consent checking)
    orchestrator_normalized.py     # Polls OW normalized API
    orchestrator_raw.py            # Reads raw payloads from MinIO S3
    raw_payload_reader.py          # boto3 wrapper for MinIO

core/services/ow_integration.py   # OW API client (user management, OAuth, polling fetch)
core/views/ow.py                  # Provider list + OAuth callback proxy
core/views/ow_webhook.py          # Webhook receiver (dormant unless ingest_mode=webhook)
core/views/patient.py             # Patient wearable actions (connect, sync, revoke)
core/management/commands/ow_poll.py  # Cron entry point

scripts/
    sandbox_test.sh                # Host-side sandbox smoke test
    ow_ordered_sync.py             # Runs inside OW container for ordered sync

patient-frontend/                  # React SPA for patient consent + wearable connection
```

## Cron Schedule

The `deploy/crontab` file runs `ow_poll` daily at 6am UTC. The Docker Compose `jhe_cron` service (profile: `polling`) uses supercronic to execute it.
