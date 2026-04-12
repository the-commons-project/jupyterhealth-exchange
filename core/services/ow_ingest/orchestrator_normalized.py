"""OW normalized polling pipeline orchestrator.

Ingests normalized OW data (summaries, timeseries, sleep details) via
omh-shim and stores as FHIR Observations. Shared helpers live in
``_common``; this module owns the data-type definitions and the
fetch/ingest loop.

Public API: ``ingest_for_user(patient_id, ...)``.
"""

import logging
from datetime import UTC, datetime, timedelta

import omh_shim
import requests
from django.utils import timezone as django_timezone

from core.models import (
    CodeableConcept,
    Observation,
    OWPollEvent,
    OWPollStatus,
    Patient,
)
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
from core.services.ow_integration import load_and_validate_polling_config, ow_service

logger = logging.getLogger(__name__)

# The seven omh-shim v0.1 data types we ingest from OW. Tuple so it
# can't be mutated and so the iteration order is stable.
DATA_TYPES = (
    "heart_rate",
    "heart_rate_variability",
    "oxygen_saturation",
    "step_count",
    "sleep_duration",
    "sleep_episode",
    "physical_activity",
)

SUMMARY_DATA_TYPES = frozenset({"step_count", "sleep_duration", "physical_activity"})
TIMESERIES_DATA_TYPES = frozenset({"heart_rate", "heart_rate_variability", "oxygen_saturation"})

_DATA_TYPE_TO_CACHE_KEY = {
    **{dt: "timeseries" for dt in TIMESERIES_DATA_TYPES},
    **{dt: "summaries" for dt in SUMMARY_DATA_TYPES},
    "sleep_episode": "sleep_details",
}
if set(_DATA_TYPE_TO_CACHE_KEY) != set(DATA_TYPES):
    raise RuntimeError(f"cache key drift: {set(_DATA_TYPE_TO_CACHE_KEY) ^ set(DATA_TYPES)}")

DATA_TYPE_TO_CODE = {
    "heart_rate": "omh:heart-rate:2.0",
    "heart_rate_variability": "omh:heart-rate-variability:1.0",
    "oxygen_saturation": "omh:oxygen-saturation:2.0",
    "step_count": "omh:step-count:3.0",
    "sleep_duration": "omh:sleep-duration:2.0",
    "sleep_episode": "omh:sleep-episode:1.1",
    "physical_activity": "omh:physical-activity:1.2",
}


def compute_window(
    poll_status: OWPollStatus, cfg: dict, now: datetime, *, force_backfill: bool = False
) -> tuple[datetime, datetime]:
    """Return ``(start, end)`` for an OW fetch.

    Backfill window applies when (a) ``force_backfill`` is set, or (b) this
    tuple has never had a non-empty successful poll. Once we've actually
    ingested at least one record we shrink to the lookback window.
    """
    use_backfill = force_backfill or not poll_status.backfill_complete
    days = cfg["initial_backfill_days"] if use_backfill else cfg["lookback_days"]
    return now - timedelta(days=days), now


def _fetch_for_data_type(data_type: str, ow_user_id: str, start: datetime, end: datetime, cache: dict) -> list[dict]:
    """Fetch the right OW endpoint, using the per-patient cache so we only
    call /summaries and /timeseries once per cron tick even when multiple
    data_types share an endpoint. Empty lists from 404s are also cached so
    sibling data_types short-circuit.
    """
    if data_type in TIMESERIES_DATA_TYPES:
        if "timeseries" not in cache:
            cache["timeseries"] = ow_service.fetch_timeseries(ow_user_id, list(TIMESERIES_DATA_TYPES), start, end)
        return [s for s in cache["timeseries"] if s.get("type") == data_type]
    if data_type in SUMMARY_DATA_TYPES:
        if "summaries" not in cache:
            cache["summaries"] = ow_service.fetch_summaries(ow_user_id, start, end)
        return cache["summaries"]
    if data_type == "sleep_episode":
        if "sleep_details" not in cache:
            cache["sleep_details"] = ow_service.fetch_sleep_details(ow_user_id, start, end)
        return cache["sleep_details"]
    raise ValueError(f"unknown data_type: {data_type}")


def _save_poll_error(
    poll_status: OWPollStatus,
    now: datetime,
    err: str,
) -> tuple[int, int, str]:
    """Set an HTTP-level error on the poll status row and save. Returns the
    standard ``(ingested, skipped, err)`` tuple so callers can ``return`` it.
    """
    truncated = _truncate(err, ERROR_STRING_MAX_LEN)
    poll_status.last_poll_at = now
    poll_status.last_error = truncated
    poll_status.save()
    return 0, 0, truncated


def _ingest_one_data_type(
    patient: Patient,
    data_type: str,
    ow_user_id: str,
    cfg: dict,
    cache: dict,
    data_source,
    *,
    dry_run: bool,
    force_backfill: bool,
) -> tuple[int, int, str | None]:
    """Ingest one (patient, data_type). Returns (ingested, skipped, error).
    HTTP errors are fail-fast; record errors are fail-soft."""
    if dry_run:
        try:
            poll_status = OWPollStatus.objects.get(patient=patient, data_type=data_type)
        except OWPollStatus.DoesNotExist:
            poll_status = OWPollStatus(patient=patient, data_type=data_type)
    else:
        poll_status, _ = OWPollStatus.objects.get_or_create(patient=patient, data_type=data_type)

    if poll_status.disabled:
        return 0, 0, None
    now = django_timezone.now()
    start, end = compute_window(poll_status, cfg, now, force_backfill=force_backfill)

    try:
        samples = _fetch_for_data_type(data_type, ow_user_id, start, end, cache)
    except requests.RequestException as e:
        status_code = e.response.status_code if isinstance(e, requests.HTTPError) and e.response is not None else None
        if status_code == 404:
            samples = []
            cache.setdefault(_DATA_TYPE_TO_CACHE_KEY[data_type], [])
        else:
            err_msg = f"OW fetch failed: {e}"
            if dry_run:
                return 0, 0, _truncate(err_msg, ERROR_STRING_MAX_LEN)
            return _save_poll_error(poll_status, now, err_msg)
    except (ValueError, KeyError, TypeError) as e:
        err_msg = f"OW returned unexpected shape for {data_type}: {e}"
        if dry_run:
            return 0, 0, _truncate(err_msg, ERROR_STRING_MAX_LEN)
        return _save_poll_error(poll_status, now, err_msg)

    try:
        codeable_concept = get_codeable_concept(DATA_TYPE_TO_CODE[data_type])
    except CodeableConcept.DoesNotExist:
        err_msg = (
            f"missing CodeableConcept for {data_type} ({DATA_TYPE_TO_CODE[data_type]}) "
            "— check migration 0020 was applied"
        )
        if dry_run:
            return 0, 0, _truncate(err_msg, ERROR_STRING_MAX_LEN)
        return _save_poll_error(poll_status, now, err_msg)

    existing_dts = fetch_existing_effective_dts(patient, codeable_concept, data_source)
    system_user = get_system_user()
    ingested = 0
    skipped = 0

    for idx, sample in enumerate(samples):
        try:
            omh_record = omh_shim.convert(
                source="ow_normalized",
                data_type=data_type,
                sample=sample,
                tz=UTC if data_type in SUMMARY_DATA_TYPES else None,
            )
        except Exception as e:
            logger.warning(
                "ow_ingest convert failed: patient=%s data_type=%s idx=%s err=%s",
                patient.id,
                data_type,
                idx,
                e,
            )
            skipped += 1
            continue

        body = omh_record.get("body", omh_record)
        effective_dt = _extract_effective_dt(body)
        if effective_dt is None:
            logger.warning(
                "ow_ingest skipped: patient=%s data_type=%s idx=%s reason=no_effective_time",
                patient.id,
                data_type,
                idx,
            )
            skipped += 1
            continue
        if effective_dt in existing_dts:
            continue  # dedup hit; not a skip

        if dry_run:
            logger.info(
                "ow_ingest dry-run: would ingest patient=%s data_type=%s effective=%s",
                patient.id,
                data_type,
                effective_dt,
            )
            ingested += 1
            existing_dts.add(effective_dt)
            continue

        fhir_dict = wrap_omh_as_fhir(omh_record, patient, data_source, codeable_concept)
        try:
            Observation.fhir_create(fhir_dict, system_user)
            ingested += 1
            existing_dts.add(effective_dt)
        except Exception as e:  # noqa: BLE001 — fail-soft per record
            logger.warning(
                "ow_ingest fhir_create failed: patient=%s data_type=%s idx=%s err=%s",
                patient.id,
                data_type,
                idx,
                e,
            )
            skipped += 1

    if not dry_run:
        poll_status.last_poll_at = now
        poll_status.last_success_at = now
        if samples:
            poll_status.backfill_complete = True
        poll_status.last_error = None
        poll_status.save()
    return ingested, skipped, None


def ingest_for_user(
    patient_id: int,
    *,
    trigger: str = "cron",
    dry_run: bool = False,
    force_backfill: bool = False,
) -> tuple[int, int, bool]:
    """Run the polling pipeline for one patient. Returns (ingested, skipped, errored)."""
    patient = Patient.objects.select_related("jhe_user").get(pk=patient_id)
    cfg = load_and_validate_polling_config()
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

    cache: dict = {}
    total_ingested = 0
    total_skipped = 0
    errors: list[str] = []

    for data_type in DATA_TYPES:
        if data_type not in allowed:
            continue
        ing, skip, err = _ingest_one_data_type(
            patient,
            data_type,
            ow_user_id,
            cfg,
            cache,
            data_source,
            dry_run=dry_run,
            force_backfill=force_backfill,
        )
        total_ingested += ing
        total_skipped += skip
        if err is not None:
            errors.append(f"{data_type}: {err}")

    event.records_ingested = total_ingested
    event.records_skipped = total_skipped
    event.completed_at = django_timezone.now()
    event.status = "errored" if errors else "completed"
    if errors:
        event.error_message = _truncate(" | ".join(errors), ERROR_MESSAGE_MAX_LEN)
    event.save()

    return total_ingested, total_skipped, bool(errors)
