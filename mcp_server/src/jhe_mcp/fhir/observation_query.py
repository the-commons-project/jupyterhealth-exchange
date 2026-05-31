from __future__ import annotations

from typing import Any

from jhe_mcp.fhir.client import JheClient
from jhe_mcp.fhir.models import Observation
from jhe_mcp.omh_registry import all_short_names, lookup_code

MAX_PAGE_SIZE = 1000


def build_observation_params(
    *,
    patient_id: str | None = None,
    study_id: str | None = None,
    data_type: str | None = None,
) -> dict[str, Any]:
    """Build FHIR Observation query params shared by all observation tools.

    Date filtering is intentionally NOT included here: the JHE FHIR Observation
    endpoint does not parse a ``date`` parameter, so any date window is applied
    client-side (see ``in_date_range`` / ``collect_observations``).
    """
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
    return params


def in_date_range(effective_at: str | None, start: str | None, end: str | None) -> bool:
    """Inclusive date-window check on an observation's effective timestamp.

    Compares the date portion (``YYYY-MM-DD``) of an ISO-8601 ``effective_at``
    against ``start``/``end`` (also ``YYYY-MM-DD``). Undated observations
    (``effective_at is None``) are treated as out of range when a window is
    given, since they cannot be placed in time.
    """
    if effective_at is None:
        return False
    day = effective_at[:10]
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


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
    """Page through every matching entry server-side (raw bundle entries)."""
    out: list[dict] = []
    page = 1
    while True:
        total, entries, has_more = await fetch_observation_page(client, params, page=page, page_size=MAX_PAGE_SIZE)
        out.extend(entries)
        if not has_more or not entries:
            break
        page += 1
    return out


async def collect_observations(
    client: JheClient,
    params: dict[str, Any],
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[Observation]:
    """Fetch all matching observations, applying a client-side date window.

    The backend ignores date params, so when ``start``/``end`` are supplied we
    fetch the full (patient/study/code-scoped) set and filter in process on each
    record's ``effective_at``.
    """
    entries = await iter_all_observations(client, params)
    observations = [Observation.from_fhir_entry(e) for e in entries]
    if start or end:
        observations = [o for o in observations if in_date_range(o.effective_at, start, end)]
    return observations


async def count_with_optional_date(
    client: JheClient,
    params: dict[str, Any],
    start: str | None,
    end: str | None,
) -> int:
    """Count observations, using the cheap bundle `total` when no date window is
    given, and a client-side filtered full fetch when one is."""
    if not (start or end):
        return await count_observations(client, params)
    return len(await collect_observations(client, params, start=start, end=end))
