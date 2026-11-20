"""One vendor's SourceSettings, built from what the host's directory row says (JHE: the `EhrVendor` row).

Every vendor is pulled with the one USCDI query table (`profiles.py`). What differs between vendors
is set per row by the host, so an operator can change it without a release: the client id, scopes
added to the base set, the SMART scope version, whether the vendor's own screen asks the patient
which record types to share, and replacement searches for individual rows of the query table.

A replacement search is free text an operator types, so `check_query_override` refuses one that
could pull more than its row's scope grants or leave the vendor's FHIR server. Hosts call it when
the row is saved; `vendor_settings` calls it again so a row written some other way cannot slip by.
"""

from __future__ import annotations

from collections.abc import Mapping

from .profiles import USCDI_PROFILE
from .source_settings import BASE_SCOPES, ClientRegistration, FetchProfile, QuerySpec, SourceSettings
from .vocab import ScopeVersion, TypeChoice


def overridable_rows(profile: FetchProfile = USCDI_PROFILE) -> tuple[QuerySpec, ...]:
    """The rows a host may give a replacement search, in table order. An instance read (the
    patient's own Patient resource) is left out: it has no search to replace."""
    return tuple(q for q in profile.queries if not q.single)


def check_query_override(label: str, query: str, profile: FetchProfile = USCDI_PROFILE) -> None:
    """Raise ValueError unless `query` may replace the search of the row named `label`.

    The search must stay on the row's own resource type, since the row's scope is what decides
    whether it runs: `Observation` under the Documents row would pull Observations on a
    DocumentReference grant. It must also be a path relative to the vendor's FHIR base, never an
    absolute URL or a step out of it. The messages never repeat the input, so a host can show them
    as they are."""
    row = next((q for q in overridable_rows(profile) if q.label == label), None)
    if row is None:
        raise ValueError("Unknown query row; pick one from the list.")
    if not query or query != query.strip() or any(c.isspace() for c in query):
        raise ValueError(f"The {row.label} search must be non-empty with no spaces.")
    path = query.split("?", 1)[0]
    if "://" in query or query.startswith("/") or ".." in path.split("/") or "#" in query:
        raise ValueError(f"The {row.label} search must be a path relative to the FHIR base, e.g. {row.query}.")
    if path.split("/", 1)[0] != row.resource_type:
        raise ValueError(f"The {row.label} search must start with {row.resource_type}.")


def vendor_settings(
    *,
    vendor_name: str,
    client_id: str,
    extra_scopes: tuple[str, ...] = (),
    type_choice: TypeChoice = TypeChoice.HOST,
    scope_version: ScopeVersion = ScopeVersion.V1,
    query_overrides: Mapping[str, str] | None = None,
    profile: FetchProfile = USCDI_PROFILE,
) -> SourceSettings:
    """One vendor's settings: the host's row (`query_overrides` is row label -> replacement search)
    applied to the query table. Raises ValueError on an override `check_query_override` refuses."""
    fetch = profile
    for label, query in (query_overrides or {}).items():
        check_query_override(label, query, profile)
        fetch = fetch.with_query(label, query, name=profile.name)
    return SourceSettings(
        registration=ClientRegistration(
            vendor=vendor_name,
            client_id=client_id,
            base_scopes=tuple(dict.fromkeys((*BASE_SCOPES, *extra_scopes))),
            scope_version=scope_version,
        ),
        fetch=fetch,
        type_choice=type_choice,
    )
