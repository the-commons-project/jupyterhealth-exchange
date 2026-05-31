from __future__ import annotations

from jhe_mcp.fhir.client import JheClient
from jhe_mcp.fhir.observation_query import build_observation_params, count_observations
from jhe_mcp.tools.study import list_study_patients


async def count_patient_observations(
    *,
    patient_id: str,
    data_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    base_url: str,
) -> int:
    """Exact number of observations for a patient (optionally filtered)."""
    params = build_observation_params(patient_id=patient_id, data_type=data_type, start=start, end=end)
    async with JheClient(base_url) as client:
        return await count_observations(client, params)


async def count_study_observations(
    *,
    study_id: str,
    data_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    by_patient: bool = False,
    base_url: str,
) -> int | dict[str, int]:
    """Observation count for a whole study, or per-patient when by_patient=True."""
    if not by_patient:
        params = build_observation_params(study_id=study_id, data_type=data_type, start=start, end=end)
        async with JheClient(base_url) as client:
            return await count_observations(client, params)

    patients = await list_study_patients(study_id=study_id, base_url=base_url)
    counts: dict[str, int] = {}
    async with JheClient(base_url) as client:
        for p in patients:
            params = build_observation_params(patient_id=p.patient_id, data_type=data_type, start=start, end=end)
            counts[p.patient_id] = await count_observations(client, params)
    return counts
