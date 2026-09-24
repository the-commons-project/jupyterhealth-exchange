from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from jhe_mcp.fhir.client import JheClient, JheClientError
from jhe_mcp.fhir.models import Demographics, StudyMeta, StudyPatient


async def get_study_count(*, base_url: str) -> int:
    """Total number of studies the caller can see."""
    async with JheClient(base_url) as client:
        data = await client.admin_get("studies", params={"page_size": 1})
        if not isinstance(data, dict) or "count" not in data:
            raise JheClientError(0, f"Expected a paginated studies response with 'count', got: {str(data)[:200]}")
        return int(data["count"])


async def _admin_get_all(client: JheClient, path: str) -> list[dict[str, Any]]:
    """Every item of a paginated Admin API list, following `next` links."""
    results: list[dict[str, Any]] = []
    params: dict[str, Any] = {}
    while True:
        data = await client.admin_get(path, params=params or None)
        results.extend(data.get("results", []))
        next_url = data.get("next")
        if not next_url:
            return results
        # Forward ALL query params from the next URL so pagination works with any
        # scheme (?page=, ?cursor=, ?offset=&limit=, etc.). parse_qs returns lists;
        # take the first value of each key to produce a flat dict for admin_get.
        next_qs = parse_qs(urlparse(next_url).query)
        params = {k: v[0] for k, v in next_qs.items()}


async def list_studies(*, base_url: str) -> list[StudyMeta]:
    """Studies the caller can see (slim summaries)."""
    async with JheClient(base_url) as client:
        return [StudyMeta.from_admin(item) for item in await _admin_get_all(client, "studies")]


async def get_study_metadata(*, study_id: str, base_url: str) -> StudyMeta | None:
    """Metadata for one study; None if not found or not authorized."""
    async with JheClient(base_url) as client:
        data = await client.admin_get(f"studies/{study_id}", treat_404_as_none=True)
        return StudyMeta.from_admin(data) if data is not None else None


async def list_study_patients(*, study_id: str, base_url: str) -> list[StudyPatient]:
    """Patients enrolled in a study."""
    async with JheClient(base_url) as client:
        return [StudyPatient.from_admin(item) for item in await _admin_get_all(client, f"studies/{study_id}/patients")]


async def get_patient_demographics(*, patient_id: str, base_url: str) -> Demographics | None:
    """Patient demographics via JHE's Admin API.

    Uses `/api/v1/patients/{id}` because the FHIR `/Patient/{id}` detail view
    rejects direct ID lookup with a 400; the Admin API endpoint works correctly.
    """
    async with JheClient(base_url) as client:
        data = await client.admin_get(f"patients/{patient_id}", treat_404_as_none=True)
        return Demographics.from_admin(data) if data is not None else None
