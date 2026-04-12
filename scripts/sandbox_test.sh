#!/usr/bin/env bash
# End-to-end smoke test using Oura sandbox data.
#
# This script ONLY works when the OW platform has OURA_USE_SANDBOX=true.
# It will refuse to run otherwise to prevent accidentally syncing against
# a real Oura account.
#
# Detects OW_PIPELINE_MODE automatically:
#   - normalized: syncs via ordered Python script (HR -> SpO2 -> sleep -> rest)
#   - raw: syncs via OW API (which writes raw payloads to MinIO S3)
#
# Prerequisites:
#   - docker compose --profile ow is running
#   - OW has OURA_USE_SANDBOX=true in its env
#   - Patient has consented via invite flow and connected Oura
#
# Usage:
#   ./scripts/sandbox_test.sh patrick@example.com
#   ./scripts/sandbox_test.sh patrick@example.com 30
set -euo pipefail

PATIENT_EMAIL="${1:?Usage: $0 <patient-email> [days]}"
DAYS="${2:-30}"
JHE="${JHE_CONTAINER:-jhe_owcombined_app}"
OW="${OW_CONTAINER:-jhe_owcombined_ow_app}"

# ── Guard: only run against sandbox ──────────────────────────────────
SANDBOX_FLAG=$(docker exec "$OW" printenv OURA_USE_SANDBOX 2>/dev/null || echo "")
if [[ "$SANDBOX_FLAG" != "true" ]]; then
    echo "ERROR: OURA_USE_SANDBOX is not 'true' in the OW container."
    echo "This script only works with the Oura sandbox to prevent"
    echo "accidentally syncing against a real Oura account."
    echo ""
    echo "Set OURA_USE_SANDBOX=true in the OW backend .env and restart."
    exit 1
fi
echo "Sandbox mode confirmed (OURA_USE_SANDBOX=true)"

# ── Detect pipeline mode ─────────────────────────────────────────────
PIPELINE_MODE=$(docker exec "$JHE" printenv OW_PIPELINE_MODE 2>/dev/null || echo "normalized")
echo "Pipeline mode: ${PIPELINE_MODE}"

# ── Step 1: Resolve patient ─────────────────────────────────────────
echo ""
echo "=== Step 1: Resolve patient ==="
RESOLVE_OUTPUT=$(docker exec "$JHE" python -c "
import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'jhe.settings')
import django; django.setup()
from core.models import Patient
from core.services.ow_ingest._common import resolve_ow_user_id
from core.jhe_settings.service import get_setting
p = Patient.objects.select_related('jhe_user').get(jhe_user__email='${PATIENT_EMAIL}')
ow_id = resolve_ow_user_id(p)
if not ow_id:
    print(f'ERROR: Patient {p.id} has no ow:* identifier. Complete the invite flow first.')
    exit(1)
api_key = get_setting('ow.api_key') or ''
api_base = (get_setting('ow.api_base_url') or '').rstrip('/')
print(f'{p.id} {ow_id} {api_key} {api_base}')
" 2>/dev/null)

if [[ "$RESOLVE_OUTPUT" == ERROR* ]]; then
    echo "$RESOLVE_OUTPUT"
    exit 1
fi

PATIENT_PK=$(echo "$RESOLVE_OUTPUT" | awk '{print $1}')
OW_USER_ID=$(echo "$RESOLVE_OUTPUT" | awk '{print $2}')
OW_API_KEY=$(echo "$RESOLVE_OUTPUT" | awk '{print $3}')
OW_API_BASE=$(echo "$RESOLVE_OUTPUT" | awk '{print $4}')

echo "Patient: ${PATIENT_EMAIL} (pk=${PATIENT_PK})"
echo "OW user ID: ${OW_USER_ID}"

# ── Step 2: Sync sandbox data ───────────────────────────────────────
echo ""
if [[ "$PIPELINE_MODE" == "raw" ]]; then
    # Raw mode: sync via OW API so raw payloads get written to MinIO.
    # Step 2a: historical sync (async, covers 90-day window)
    echo "=== Step 2: Sync via OW API (raw mode — writes to MinIO S3) ==="
    echo "  Triggering historical sync..."
    HIST_RESP=$(docker exec "$JHE" python -c "
import requests
resp = requests.post('${OW_API_BASE}/api/v1/providers/oura/users/${OW_USER_ID}/sync/historical',
    headers={'X-Open-Wearables-API-Key': '${OW_API_KEY}'}, timeout=30)
print(f'{resp.status_code}')
" 2>/dev/null)
    echo "  Historical sync: ${HIST_RESP}"

    echo "  Waiting 15s for async sync to complete..."
    sleep 15

    # Step 2b: follow-up sync (synchronous, picks up heartrate)
    echo "  Triggering follow-up sync (synchronous)..."
    SYNC_RESP=$(docker exec "$JHE" python -c "
import requests
resp = requests.post('${OW_API_BASE}/api/v1/providers/oura/users/${OW_USER_ID}/sync',
    headers={'X-Open-Wearables-API-Key': '${OW_API_KEY}'},
    params={'async': 'false'}, timeout=120)
print(f'{resp.status_code}')
" 2>/dev/null)
    echo "  Follow-up sync: ${SYNC_RESP}"

    # Verify payloads in MinIO
    S3_COUNT=$(docker exec "$JHE" python -c "
import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'jhe.settings')
import django; django.setup()
from django.core.cache import cache; cache.clear()
from core.services.ow_ingest.raw_payload_reader import list_new_objects
from datetime import datetime, timezone, timedelta
objects = list_new_objects('${OW_USER_ID}', datetime.now(timezone.utc) - timedelta(days=30))
print(len(objects))
for obj in objects:
    trace = obj.metadata.get('trace_id', 'NONE')
    print(f'  {obj.key.split(\"/\")[-1]}: {trace}')
" 2>/dev/null)
    echo "  S3 objects: $(echo "$S3_COUNT" | head -1)"
    echo "$S3_COUNT" | tail -n +2
else
    # Normalized mode: sync via ordered Python script (direct DB writes).
    echo "=== Step 2: Sync via ordered script (normalized mode) ==="
    docker cp "$(dirname "$0")/ow_ordered_sync.py" "${OW}:/root_project/scripts/ow_ordered_sync.py"
    docker exec "$OW" python /root_project/scripts/ow_ordered_sync.py "$OW_USER_ID" --days "$DAYS"
fi

# ── Step 3: Ingest into JHE ─────────────────────────────────────────
echo ""
echo "=== Step 3: Ingest into JHE via ow_poll ==="
docker exec "$JHE" python manage.py ow_poll --patient-id "$PATIENT_PK"

# ── Step 4: Summary ─────────────────────────────────────────────────
echo ""
echo "=== Step 4: Summary ==="
docker exec "$JHE" python -c "
import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'jhe.settings')
import django; django.setup()
from core.models import Observation, OWPollEvent, Patient
p = Patient.objects.get(jhe_user__email='${PATIENT_EMAIL}')
obs = Observation.objects.filter(subject_patient=p)
print(f'Total observations: {obs.count()}')
codes = obs.values_list('codeable_concept__coding_code', flat=True).distinct()
for code in sorted(codes):
    count = obs.filter(codeable_concept__coding_code=code).count()
    print(f'  {code}: {count}')
event = OWPollEvent.objects.filter(patient=p).order_by('-started_at').first()
if event:
    print(f'Last event: status={event.status} ingested={event.records_ingested} skipped={event.records_skipped}')
    if event.error_message:
        print(f'  error: {event.error_message[:200]}')
" 2>/dev/null

echo ""
echo "Done."
