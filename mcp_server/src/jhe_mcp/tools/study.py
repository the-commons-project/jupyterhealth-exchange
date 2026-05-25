from __future__ import annotations

from typing import Any

from jhe_mcp.fhir.client import JheClient
from jhe_mcp.fhir.models import Demographics, Observation, StudyMeta, StudyPatient

# Minimal OMH short-name → FHIR/LOINC code map. Full registry lands in M2
# alongside OMH schema Resources. Each value is "system|code" — passes
# straight to FHIR's token-search syntax.
_OMH_CODE_MAP: dict[str, str] = {
    "blood-glucose": "http://loinc.org|2339-0",
    "heart-rate": "http://loinc.org|8867-4",
    "body-weight": "http://loinc.org|29463-7",
    "body-temperature": "http://loinc.org|8310-5",
}


def get_data_schema_description() -> dict:
    """Return a description of how observation data is structured in JHE."""
    return {
        "overview": (
            "JupyterHealth Exchange stores health observations as FHIR R5 Observation "
            "resources. Each observation has a code (identifying the measurement type) "
            "and a valueAttachment containing the actual measurement data in Open mHealth "
            "(OMH) JSON format."
        ),
        "fhir_observation_fields": {
            "id": "Unique observation identifier",
            "subject": "Reference to the patient (e.g. {'reference': 'Patient/40001'})",
            "code": {
                "description": "Identifies the type of measurement",
                "structure": {
                    "coding": [{"system": "https://w3id.org/openmhealth", "code": "omh:<type>:<version>"}],
                },
                "known_types": [
                    "omh:blood-pressure:4.0",
                    "omh:heart-rate:2.0",
                    "omh:blood-glucose:4.0",
                    "omh:body-temperature:4.0",
                    "omh:oxygen-saturation:2.0",
                    "omh:respiratory-rate:2.0",
                ],
            },
            "valueAttachment": {
                "description": (
                    "The actual measurement data in OMH format. This is a JSON object "
                    "with 'header' (metadata) and 'body' (measurements). The body "
                    "structure varies by measurement type."
                ),
            },
        },
        "omh_body_examples": {
            "blood-pressure": {
                "systolic_blood_pressure": {"unit": "mmHg", "value": 120},
                "diastolic_blood_pressure": {"unit": "mmHg", "value": 80},
                "effective_time_frame": {"date_time": "2026-01-15T08:00:00+00:00"},
            },
            "heart-rate": {
                "heart_rate": {"unit": "beats/min", "value": 72},
                "effective_time_frame": {"date_time": "2026-01-15T08:00:00+00:00"},
            },
            "blood-glucose": {
                "blood_glucose": {"unit": "mg/dL", "value": 95},
                "effective_time_frame": {"date_time": "2026-01-15T08:00:00+00:00"},
            },
            "body-temperature": {
                "body_temperature": {"unit": "C", "value": 36.8},
                "effective_time_frame": {"date_time": "2026-01-15T08:00:00+00:00"},
            },
        },
        "how_to_read_values": (
            "The FHIR Observation's top-level valueQuantity fields may be empty. "
            "The actual measurement values are always in the valueAttachment.body "
            "object. Each measurement type has its own body structure — see the "
            "omh_body_examples above for the fields specific to each type."
        ),
    }


async def get_study_count(*, base_url: str) -> int:
    """Total number of studies the caller can see."""
    async with JheClient(base_url) as client:
        data = await client.admin_get("studies", params={"page_size": 1})
        return int(data.get("count", 0))


async def list_studies(*, base_url: str) -> list[StudyMeta]:
    """Studies the caller can see (slim summaries)."""
    results: list[StudyMeta] = []
    async with JheClient(base_url) as client:
        params: dict[str, Any] = {}
        while True:
            data = await client.admin_get("studies", params=params or None)
            results.extend(StudyMeta.from_admin(item) for item in data.get("results", []))
            next_url = data.get("next")
            if not next_url:
                break
            from urllib.parse import parse_qs, urlparse

            next_params = parse_qs(urlparse(next_url).query)
            params = {"page": next_params["page"][0]}
    return results


async def get_study_metadata(*, study_id: str, base_url: str) -> StudyMeta | None:
    """Metadata for one study; None if not found or not authorized."""
    async with JheClient(base_url) as client:
        data = await client.admin_get(f"studies/{study_id}", treat_404_as_none=True)
        return StudyMeta.from_admin(data) if data is not None else None


async def list_study_patients(*, study_id: str, base_url: str) -> list[StudyPatient]:
    """Patients enrolled in a study."""
    async with JheClient(base_url) as client:
        data = await client.admin_get(f"studies/{study_id}/patients")
        return [StudyPatient.from_admin(item) for item in data]


async def get_patient_demographics(*, patient_id: str, base_url: str) -> Demographics | None:
    """Patient demographics via JHE's Admin API.

    Uses `/api/v1/patients/{id}` because the FHIR `/Patient/{id}` detail view
    400s on direct ID lookup (spike finding 2026-05-19).
    """
    async with JheClient(base_url) as client:
        data = await client.admin_get(f"patients/{patient_id}", treat_404_as_none=True)
        return Demographics.from_admin(data) if data is not None else None


async def get_patient_observations(
    *,
    patient_id: str,
    data_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    base_url: str,
) -> list[Observation]:
    """FHIR Observations for a patient, optionally filtered."""
    params: dict[str, Any] = {"patient": patient_id}
    if data_type:
        code = _OMH_CODE_MAP.get(data_type)
        if code is None:
            raise ValueError(f"Unknown data_type {data_type!r}. Known: {sorted(_OMH_CODE_MAP)}")
        params["code"] = code
    if start and end:
        params["date"] = [f"ge{start}", f"le{end}"]
    elif start:
        params["date"] = f"ge{start}"
    elif end:
        params["date"] = f"le{end}"
    async with JheClient(base_url) as client:
        bundle = await client.fhir_get("Observation", params=params)
        return [Observation.from_fhir_entry(e) for e in bundle.get("entry", [])]
