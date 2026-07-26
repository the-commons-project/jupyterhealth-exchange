from __future__ import annotations

from typing import Any

from jhe_mcp.fhir.client import JheClient
from jhe_mcp.fhir.models import Observation, SlimObservation
from jhe_mcp.fhir.observation_query import (
    build_observation_params,
    collect_observations,
    count_observations,
    fetch_observation_page,
)

_ORDER_TO_SORT = {"newest": "-date", "oldest": "date"}


async def summarize_patient_observations(
    *,
    patient_id: str,
    start: str | None = None,
    end: str | None = None,
    base_url: str,
) -> dict[str, dict[str, Any]]:
    """Per-data-type digest for a patient: {type: {count, earliest, latest}}.

    Aggregation is client-side (the server has no group-by, and the summary must
    include data types outside the OMH registry), but any date window bounds the
    fetch server-side.
    """
    params = build_observation_params(patient_id=patient_id, start=start, end=end)
    async with JheClient(base_url) as client:
        observations = await collect_observations(client, params)
    summary: dict[str, dict[str, Any]] = {}
    for obs in observations:
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


async def get_patient_date_range(
    *,
    patient_id: str,
    base_url: str,
) -> dict[str, Any]:
    """Earliest/latest observation timestamp and total count for a patient.

    Three cheap server-side queries: ``_summary=count`` for the total, then a
    single record sorted ascending / descending by effective date. No paging.
    ``earliest``/``latest`` are ISO-8601 strings (``None`` when there are no
    records or the boundary record has no parseable effective time).
    """
    params = build_observation_params(patient_id=patient_id)
    async with JheClient(base_url) as client:
        count = await count_observations(client, params)
        if count == 0:
            return {"earliest": None, "latest": None, "count": 0}
        _, first_entries, _ = await fetch_observation_page(client, params, page=1, page_size=1, sort="date")
        _, last_entries, _ = await fetch_observation_page(client, params, page=1, page_size=1, sort="-date")
    earliest = Observation.from_fhir_entry(first_entries[0]).effective_at if first_entries else None
    latest = Observation.from_fhir_entry(last_entries[0]).effective_at if last_entries else None
    return {"earliest": earliest, "latest": latest, "count": count}


async def get_patient_observations(
    *,
    patient_id: str,
    data_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
    verbosity: str = "slim",
    order: str = "newest",
    limit: int = 50,
    page: int = 1,
    base_url: str,
) -> dict[str, Any]:
    """One page of a patient's observations with total/has_more awareness.

    verbosity="slim" (default) omits the raw OMH body; "full" includes it.
    Date windows and ordering are applied server-side (FHIR ``date`` + ``_sort``).
    """
    if verbosity not in ("slim", "full"):
        raise ValueError(f"verbosity must be 'slim' or 'full', got {verbosity!r}")
    sort = _ORDER_TO_SORT.get(order)
    if sort is None:
        raise ValueError(f"order must be 'newest' or 'oldest', got {order!r}")
    params = build_observation_params(patient_id=patient_id, data_type=data_type, start=start, end=end)
    page_size = max(1, min(int(limit), 1000))
    page = max(1, int(page))
    async with JheClient(base_url) as client:
        total, entries, has_more = await fetch_observation_page(
            client, params, page=page, page_size=page_size, sort=sort
        )
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
