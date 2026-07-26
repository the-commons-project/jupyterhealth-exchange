"""Patient lookup via the JHE FHIR Patient search API (JHE PR #667)."""

from __future__ import annotations

from datetime import date
from typing import Any

from jhe_mcp.fhir.client import JheClient
from jhe_mcp.fhir.models import PatientSearchResult
from jhe_mcp.fhir.observation_query import _bundle_total

_DATE_PREFIXES = ("ge", "le", "gt", "lt")


def build_patient_params(
    *,
    name: str | None = None,
    family: str | None = None,
    given: str | None = None,
    birthdate: str | None = None,
) -> dict[str, Any]:
    """FHIR Patient search params. String params are case-insensitive prefix matches.

    ``birthdate`` is ``YYYY-MM-DD`` with an optional ``ge``/``le``/``gt``/``lt``
    prefix (bare value = exact day). Raises ``ValueError`` when no criterion is
    given or the birthdate is malformed, so the tool fails before any request.
    """
    params: dict[str, Any] = {}
    if name:
        params["name"] = name
    if family:
        params["family"] = family
    if given:
        params["given"] = given
    if birthdate:
        value = birthdate[2:] if birthdate[:2] in _DATE_PREFIXES else birthdate
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(
                f"birthdate must be YYYY-MM-DD with an optional ge/le/gt/lt prefix; got {birthdate!r}"
            ) from None
        params["birthdate"] = birthdate
    if not params:
        raise ValueError("Provide at least one of: name, family, given, birthdate.")
    return params


async def search_patients(
    *,
    name: str | None = None,
    family: str | None = None,
    given: str | None = None,
    birthdate: str | None = None,
    limit: int = 50,
    page: int = 1,
    base_url: str,
) -> dict[str, Any]:
    """One page of matching patients: {total, page, page_size, returned, has_more, patients}."""
    params = build_patient_params(name=name, family=family, given=given, birthdate=birthdate)
    page_size = max(1, min(int(limit), 1000))
    page = max(1, int(page))
    async with JheClient(base_url) as client:
        bundle = await client.fhir_get("Patient", params={**params, "_count": page_size, "_page": page})
    total = _bundle_total(bundle)
    entries = bundle.get("entry", []) or []
    patients = [PatientSearchResult.from_fhir_entry(e).model_dump() for e in entries]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "returned": len(patients),
        "has_more": page * page_size < total,
        "patients": patients,
    }
