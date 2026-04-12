"""Shared helpers for the OW ingestion pipeline.

Both orchestrator_normalized and orchestrator_raw import from here.
The webhook view also imports ``resolve_patient_from_ow_user_id`` directly.
For ``ingest_for_user`` and ``DATA_TYPES``, use the __init__.py dispatcher.
"""

import base64
import json
import logging
from functools import cache, lru_cache

from django.db.models import Q

from core.models import (
    CodeableConcept,
    DataSource,
    JheUser,
    Observation,
    Patient,
    StudyPatientScopeConsent,
)

logger = logging.getLogger(__name__)

OMH_SYSTEM = "https://w3id.org/openmhealth"
SYSTEM_USER_EMAIL = "ow_poller@system.local"
ERROR_STRING_MAX_LEN = 1024
ERROR_MESSAGE_MAX_LEN = 4096

_TRUNCATION_MARKER = " ...truncated"


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    marker_len = len(_TRUNCATION_MARKER)
    if limit < marker_len:
        return s[:limit]
    return s[: limit - marker_len] + _TRUNCATION_MARKER


@lru_cache(maxsize=1)
def get_system_user() -> JheUser:
    return JheUser.objects.get(email=SYSTEM_USER_EMAIL)


@cache
def get_codeable_concept(coding_code: str) -> CodeableConcept:
    return CodeableConcept.objects.get(coding_system=OMH_SYSTEM, coding_code=coding_code)


def consented_data_types(patient: Patient, data_type_to_code: dict[str, str]) -> frozenset[str]:
    """Return the subset of data_types the patient has actively consented to."""
    consented_codes = set(
        StudyPatientScopeConsent.objects.filter(
            study_patient__patient=patient,
            consented=True,
        ).values_list("scope_code__coding_code", flat=True)
    )
    return frozenset(dt for dt, code in data_type_to_code.items() if code in consented_codes)


def build_polling_set():
    """Patients with an OW connection AND at least one active consent."""
    return (
        Patient.objects.filter(
            jhe_user__identifier__startswith="ow:",
            studypatient__studypatientscopeconsent__consented=True,
        )
        .distinct()
        .select_related("jhe_user")
    )


def resolve_ow_user_id(patient: Patient) -> str | None:
    """Extract the OW UUID from ``JheUser.identifier`` (format: ``ow:<uuid>``)."""
    ident = patient.jhe_user.identifier or ""
    return ident[3:] if ident.startswith("ow:") else None


def resolve_patient_from_ow_user_id(ow_user_id: str) -> Patient | None:
    """Inverse of ``resolve_ow_user_id``."""
    return Patient.objects.select_related("jhe_user").filter(jhe_user__identifier=f"ow:{ow_user_id}").first()


def _extract_effective_dt(omh_record: dict) -> str | None:
    """Pull the ISO datetime from an OMH record's effective_time_frame."""
    etf = omh_record.get("effective_time_frame") or {}
    return etf.get("date_time") or (etf.get("time_interval") or {}).get("start_date_time")


def wrap_omh_as_fhir(
    omh_record: dict,
    patient: Patient,
    data_source: DataSource,
    codeable_concept: CodeableConcept,
) -> dict:
    """OMH data-point -> FHIR Observation dict."""
    body = omh_record.get("body", omh_record)
    effective_dt = _extract_effective_dt(body)

    return {
        "resourceType": "Observation",
        "status": "final",
        "subject": {"reference": f"Patient/{patient.id}"},
        "device": {"reference": f"Device/{data_source.id}"},
        "code": {
            "coding": [
                {
                    "system": codeable_concept.coding_system,
                    "code": codeable_concept.coding_code,
                }
            ]
        },
        "effectiveDateTime": effective_dt,
        "valueAttachment": {
            "contentType": "application/json",
            "data": base64.b64encode(json.dumps(omh_record).encode("utf-8")).decode("ascii"),
        },
    }


def fetch_existing_effective_dts(
    patient: Patient,
    codeable_concept: CodeableConcept,
    data_source: DataSource,
) -> set[str]:
    """Batch dedup: return effective timestamps already stored for this
    (patient, code, source) tuple."""
    rows = (
        Observation.objects.filter(
            subject_patient=patient,
            codeable_concept=codeable_concept,
            data_source=data_source,
        )
        .only("value_attachment_data")
        .values_list("value_attachment_data", flat=True)
    )
    seen: set[str] = set()
    for data in rows:
        if not isinstance(data, dict):
            continue
        body = data.get("body") or {}
        etf = body.get("effective_time_frame") or {}
        dt = etf.get("date_time") or (etf.get("time_interval") or {}).get("start_date_time")
        if dt:
            seen.add(dt)
    return seen


def _resolve_data_source(patient: Patient) -> DataSource | None:
    """First personal_device|device DataSource on the patient's studies."""
    return (
        DataSource.objects.filter(
            Q(type="personal_device") | Q(type="device"),
            studydatasource__study__studypatient__patient=patient,
        )
        .order_by("id")
        .first()
    )
