"""Raw Oura payload ingestion orchestrator.

Reads raw Oura API responses from MinIO, identifies data types via
trace_id metadata, shims to OMH, and stores as FHIR Observations.

Public API: ``ingest_for_user(patient_id, ...)``.
"""

import logging
from datetime import timedelta

import omh_shim
from django.utils import timezone as django_timezone

from core.models import CodeableConcept, Observation, OWPollEvent, OWPollStatus, Patient
from core.services.ow_ingest._common import (
    ERROR_MESSAGE_MAX_LEN,
    ERROR_STRING_MAX_LEN,
    _extract_effective_dt,
    _resolve_data_source,
    _truncate,
    consented_data_types,
    fetch_existing_effective_dts,
    get_codeable_concept,
    get_system_user,
    resolve_ow_user_id,
    wrap_omh_as_fhir,
)
from core.services.ow_ingest.raw_payload_reader import list_new_objects, read_object
from core.services.ow_integration import load_and_validate_polling_config

logger = logging.getLogger(__name__)

TRACE_ID_TO_DATA_TYPE = {
    "/v2/usercollection/heartrate": "heart_rate",
}

DATA_TYPES = tuple(TRACE_ID_TO_DATA_TYPE.values())

DATA_TYPE_TO_CODE = {
    "heart_rate": "omh:heart-rate:2.0",
}


def _ingest_s3_object(patient, data_type, s3_key, data_source, *, dry_run) -> tuple[int, int, str | None]:
    """Ingest one S3 object. Returns (ingested, skipped, error)."""
    coding_code = DATA_TYPE_TO_CODE.get(data_type)
    if not coding_code:
        return 0, 0, f"no coding_code mapping for data_type={data_type}"

    try:
        codeable_concept = get_codeable_concept(coding_code)
    except CodeableConcept.DoesNotExist:
        return 0, 0, f"missing CodeableConcept for {data_type} ({coding_code})"

    try:
        raw_payload = read_object(s3_key)
    except Exception as e:
        return 0, 0, _truncate(f"S3 read failed for {s3_key}: {e}", ERROR_STRING_MAX_LEN)

    records = raw_payload.get("data", [])
    if not isinstance(records, list):
        return 0, 0, f"unexpected payload shape in {s3_key}: 'data' is not a list"

    existing_dts = fetch_existing_effective_dts(patient, codeable_concept, data_source)
    system_user = get_system_user()
    ingested = 0
    skipped = 0

    for idx, record in enumerate(records):
        try:
            omh_record = omh_shim.convert(source="oura_raw", data_type=data_type, sample=record)
        except Exception as e:
            logger.warning("raw_ingest convert failed: patient=%s s3_key=%s idx=%s err=%s", patient.id, s3_key, idx, e)
            skipped += 1
            continue

        body = omh_record.get("body", omh_record)
        effective_dt = _extract_effective_dt(body)
        if effective_dt is None:
            logger.warning(
                "raw_ingest skipped: patient=%s s3_key=%s idx=%s reason=no_effective_time", patient.id, s3_key, idx
            )
            skipped += 1
            continue
        if effective_dt in existing_dts:
            continue

        if dry_run:
            ingested += 1
            existing_dts.add(effective_dt)
            continue

        fhir_dict = wrap_omh_as_fhir(omh_record, patient, data_source, codeable_concept)
        try:
            Observation.fhir_create(fhir_dict, system_user)
            ingested += 1
            existing_dts.add(effective_dt)
        except Exception as e:
            logger.warning(
                "raw_ingest fhir_create failed: patient=%s s3_key=%s idx=%s err=%s", patient.id, s3_key, idx, e
            )
            skipped += 1

    return ingested, skipped, None


def ingest_for_user(
    patient_id: int,
    *,
    trigger: str = "cron",
    dry_run: bool = False,
    force_backfill: bool = False,
) -> tuple[int, int, bool]:
    """Run the raw payload ingestion pipeline for one patient."""
    patient = Patient.objects.select_related("jhe_user").get(pk=patient_id)
    event = OWPollEvent.objects.create(patient=patient, trigger=trigger, status="started")

    def _fail_event(msg: str) -> tuple[int, int, bool]:
        event.status = "errored"
        event.error_message = _truncate(msg, ERROR_MESSAGE_MAX_LEN)
        event.completed_at = django_timezone.now()
        event.save()
        return 0, 0, True

    ow_user_id = resolve_ow_user_id(patient)
    if not ow_user_id:
        return _fail_event(f"patient {patient.id} has no ow:* identifier")

    data_source = _resolve_data_source(patient)
    if data_source is None:
        return _fail_event(f"patient {patient.id} has no personal_device DataSource on any enrolled study")

    allowed = consented_data_types(patient, DATA_TYPE_TO_CODE)
    if not allowed:
        return _fail_event(f"patient {patient.id} has no consented data types")

    cfg = load_and_validate_polling_config()
    poll_statuses = {ps.data_type: ps for ps in OWPollStatus.objects.filter(patient=patient)}

    since_candidates = [ps.last_poll_at for dt in allowed if (ps := poll_statuses.get(dt)) and ps.last_poll_at]

    if since_candidates and not force_backfill:
        since = min(since_candidates)
    else:
        since = django_timezone.now() - timedelta(days=cfg["initial_backfill_days"])

    try:
        s3_objects = list_new_objects(ow_user_id, since)
    except Exception as e:
        return _fail_event(f"S3 list failed: {e}")

    total_ingested = 0
    total_skipped = 0
    errors: list[str] = []
    now = django_timezone.now()

    for obj in s3_objects:
        trace_id = obj.metadata.get("trace_id", "")
        data_type = TRACE_ID_TO_DATA_TYPE.get(trace_id)

        if data_type is None:
            logger.debug("raw_ingest: skipping s3_key=%s trace_id=%s (not yet supported)", obj.key, trace_id)
            continue
        if data_type not in allowed:
            continue

        ing, skip, err = _ingest_s3_object(patient, data_type, obj.key, data_source, dry_run=dry_run)
        total_ingested += ing
        total_skipped += skip
        if err is not None:
            errors.append(f"{obj.key}: {err}")

    if not dry_run:
        for dt in allowed:
            ps, _ = OWPollStatus.objects.get_or_create(patient=patient, data_type=dt)
            ps.last_poll_at = now
            if total_ingested > 0 or not errors:
                ps.last_success_at = now
                ps.backfill_complete = True
                ps.last_error = None
            if errors:
                ps.last_error = _truncate(" | ".join(errors), ERROR_STRING_MAX_LEN)
            ps.save()

    event.records_ingested = total_ingested
    event.records_skipped = total_skipped
    event.completed_at = django_timezone.now()
    event.status = "errored" if errors else "completed"
    if errors:
        event.error_message = _truncate(" | ".join(errors), ERROR_MESSAGE_MAX_LEN)
    event.save()

    return total_ingested, total_skipped, bool(errors)
