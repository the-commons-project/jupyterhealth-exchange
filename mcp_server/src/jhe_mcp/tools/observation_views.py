from __future__ import annotations

from typing import Any

from jhe_mcp.fhir.client import JheClient
from jhe_mcp.fhir.models import Observation, SlimObservation
from jhe_mcp.fhir.observation_query import (
    build_observation_params,
    fetch_observation_page,
    iter_all_observations,
)


async def summarize_patient_observations(
    *,
    patient_id: str,
    start: str | None = None,
    end: str | None = None,
    base_url: str,
) -> dict[str, dict[str, Any]]:
    """Per-data-type digest for a patient: {type: {count, earliest, latest}}."""
    params = build_observation_params(patient_id=patient_id, start=start, end=end)
    async with JheClient(base_url) as client:
        entries = await iter_all_observations(client, params)
    summary: dict[str, dict[str, Any]] = {}
    for entry in entries:
        obs = Observation.from_fhir_entry(entry)
        key = obs.code_display or obs.code or "unknown"
        bucket = summary.setdefault(key, {"count": 0, "earliest": None, "latest": None})
        bucket["count"] += 1
        at = obs.effective_at
        if at:
            if bucket["earliest"] is None or at < bucket["earliest"]:
                bucket["earliest"] = at
            if bucket["latest"] is None or at > bucket["latest"]:
                bucket["latest"] = at
    return summary


async def get_patient_observations(
    *,
    patient_id: str,
    data_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    verbosity: str = "slim",
    limit: int = 50,
    page: int = 1,
    base_url: str,
) -> dict[str, Any]:
    """One page of a patient's observations with total/has_more awareness.

    verbosity="slim" (default) omits the raw OMH body; "full" includes it.
    """
    if verbosity not in ("slim", "full"):
        raise ValueError(f"verbosity must be 'slim' or 'full', got {verbosity!r}")
    params = build_observation_params(patient_id=patient_id, data_type=data_type, start=start, end=end)
    page_size = max(1, min(int(limit), 1000))
    page = max(1, int(page))
    async with JheClient(base_url) as client:
        total, entries, has_more = await fetch_observation_page(client, params, page=page, page_size=page_size)
    observations = [Observation.from_fhir_entry(e) for e in entries]
    if verbosity == "slim":
        payload = [SlimObservation.from_observation(o).model_dump() for o in observations]
    else:
        payload = [o.model_dump() for o in observations]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "returned": len(payload),
        "has_more": has_more,
        "observations": payload,
    }
