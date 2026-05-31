from __future__ import annotations

from typing import Any

from jhe_mcp.fhir.client import JheClient
from jhe_mcp.omh_registry import all_short_names, lookup_code

MAX_PAGE_SIZE = 1000


def build_observation_params(
    *,
    patient_id: str | None = None,
    study_id: str | None = None,
    data_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Build FHIR Observation query params shared by all observation tools."""
    params: dict[str, Any] = {}
    if study_id is not None:
        params["patient._has:_group:member:_id"] = study_id
    if patient_id is not None:
        params["patient"] = patient_id
    if data_type:
        code = lookup_code(data_type)
        if code is None:
            raise ValueError(f"Unknown data_type {data_type!r}. Known: {all_short_names()}")
        params["code"] = code
    if start and end:
        params["date"] = [f"ge{start}", f"le{end}"]
    elif start:
        params["date"] = f"ge{start}"
    elif end:
        params["date"] = f"le{end}"
    return params


async def count_observations(client: JheClient, params: dict[str, Any]) -> int:
    """Exact count via the bundle `total`, requesting a single record."""
    bundle = await client.fhir_get("Observation", params={**params, "_count": 1})
    return int(bundle.get("total", 0))


async def fetch_observation_page(
    client: JheClient,
    params: dict[str, Any],
    *,
    page: int,
    page_size: int,
) -> tuple[int, list[dict], bool]:
    """Return (total, entries, has_more) for one FHIR page."""
    bundle = await client.fhir_get("Observation", params={**params, "_count": page_size, "_page": page})
    total = int(bundle.get("total", 0))
    entries = bundle.get("entry", []) or []
    has_more = page * page_size < total
    return total, entries, has_more


async def iter_all_observations(client: JheClient, params: dict[str, Any]) -> list[dict]:
    """Page through every matching entry server-side (used for summaries)."""
    out: list[dict] = []
    page = 1
    while True:
        total, entries, has_more = await fetch_observation_page(client, params, page=page, page_size=MAX_PAGE_SIZE)
        out.extend(entries)
        if not has_more or not entries:
            break
        page += 1
    return out
