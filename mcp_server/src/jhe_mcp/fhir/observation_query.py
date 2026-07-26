"""Shared FHIR Observation query building, counting, and paging helpers."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from jhe_mcp.fhir.client import JheClient, JheClientError
from jhe_mcp.fhir.models import Observation
from jhe_mcp.omh_registry import all_short_names, lookup_code

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 1000
# Upper bound on pages walked by iter_all_observations, so a backend that
# reports an enormous (or wrong) `total` can't make us page indefinitely / OOM.
# MAX_PAGE_SIZE * MAX_PAGES is the most records a single call will pull.
MAX_PAGES = 50


def bundle_total(bundle: Any) -> int:
    """Return a FHIR search Bundle's ``total``, rejecting non-Bundle responses.

    JHE returns 200 with a search Bundle for FHIR queries. If the body is
    something else (an error envelope, a non-dict), reading ``total`` would
    otherwise default to 0 and silently report "no results" for what is
    actually a failure — so raise instead.
    """
    if not isinstance(bundle, dict) or "total" not in bundle:
        raise JheClientError(0, f"Expected a FHIR search Bundle with 'total', got: {str(bundle)[:200]}")
    return int(bundle["total"])


def _require_iso_date(value: str | None, label: str) -> None:
    """Validate a date-window bound is ISO ``YYYY-MM-DD``, or raise a clear error.

    Called at the param-building choke point so a malformed tool argument fails
    with an actionable message before any request is sent.
    """
    if value is None:
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD); got {value!r}") from None


def build_observation_params(
    *,
    patient_id: str | None = None,
    study_id: str | None = None,
    data_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Build FHIR Observation query params shared by all observation tools.

    ``start``/``end`` (inclusive, ``YYYY-MM-DD``) become repeated FHIR ``date``
    params (``ge{start}``/``le{end}``): day-precision values are compared at day
    precision by JHE, so both bounds are inclusive, matching the tools' contract.
    """
    _require_iso_date(start, "start")
    _require_iso_date(end, "end")
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
    date_filters = []
    if start:
        date_filters.append(f"ge{start}")
    if end:
        date_filters.append(f"le{end}")
    if date_filters:
        params["date"] = date_filters  # list value -> repeated query param (AND)
    return params


async def count_observations(client: JheClient, params: dict[str, Any]) -> int:
    """Exact count via ``_summary=count`` — the server returns only the total."""
    bundle = await client.fhir_get("Observation", params={**params, "_summary": "count"})
    return bundle_total(bundle)


async def fetch_observation_page(
    client: JheClient,
    params: dict[str, Any],
    *,
    page: int,
    page_size: int,
    sort: str | None = None,
) -> tuple[int, list[dict], bool]:
    """Return (total, entries, has_more) for one FHIR page, optionally ``_sort``-ed."""
    page_params = {**params, "_count": page_size, "_page": page}
    if sort is not None:
        page_params["_sort"] = sort
    bundle = await client.fhir_get("Observation", params=page_params)
    total = bundle_total(bundle)
    entries = bundle.get("entry", []) or []
    has_more = page * page_size < total
    return total, entries, has_more


async def iter_all_observations(client: JheClient, params: dict[str, Any]) -> list[dict]:
    """Page through every matching entry (raw bundle entries), bounded by ``MAX_PAGES``.

    Any date window is already inside ``params`` and applied server-side, so
    this walks only the (possibly windowed) match set.
    """
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        total, entries, has_more = await fetch_observation_page(client, params, page=page, page_size=MAX_PAGE_SIZE)
        out.extend(entries)
        if not has_more or not entries:
            return out
    logger.warning(
        "iter_all_observations hit MAX_PAGES=%d (%d records) for params=%s; result truncated",
        MAX_PAGES,
        len(out),
        params,
    )
    return out


async def collect_observations(client: JheClient, params: dict[str, Any]) -> list[Observation]:
    """Fetch all matching observations as parsed models (server applies any filters)."""
    entries = await iter_all_observations(client, params)
    return [Observation.from_fhir_entry(e) for e in entries]
