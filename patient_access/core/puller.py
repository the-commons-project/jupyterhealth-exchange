"""Phase 4: walk the vendor's query table, page every search, append to the spool.

Per-type isolation: one row's failure is recorded on its outcome and the walk continues. A 401
means the token is gone, so the walk stops. Resume is per resource type: the spool is per type,
so a type is refetched from its first page unless every row for it settled.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qsl

import requests

from .client import FhirClient, TokenExpired, Unauthorized
from .interfaces import SpoolHandle
from .log import JobLog, sanitised
from .models import A, Job, JsonObject, OutcomeStatus, TypeOutcome
from .source_settings import QuerySpec, SourceSettings, to_v2

SAVE_EVERY_PAGES = 5


class FetchAborted(Exception):
    """Phase 4 cannot continue (the token is gone). The message is safe to show the patient."""


def plan_outcomes(settings: SourceSettings, requested: tuple[str, ...], granted: tuple[str, ...]) -> list[TypeOutcome]:
    """One outcome per row of the query table, decided at the callback: `not_requested` when the
    patient left the type unchecked, `not_granted` when the EHR withheld a requested scope,
    `pending` for what the puller will fetch."""
    requested_set = frozenset(requested) | frozenset(to_v2(s) for s in requested)
    fetchable = {q.label for q in settings.queries_for(granted)}

    def outcome(q: QuerySpec) -> TypeOutcome:
        o = TypeOutcome(label=q.label, resource_type=q.resource_type, query=q.query)
        if q.label in fetchable:
            return o
        if q.scope in requested_set or to_v2(q.scope) in requested_set:
            o.status, o.error = OutcomeStatus.NOT_GRANTED, "the organization did not grant access to this type"
        else:
            o.status, o.error = OutcomeStatus.NOT_REQUESTED, "not selected by the patient"
        return o

    return [outcome(q) for q in settings.fetch.queries]


def search_url(base: str, spec: QuerySpec, patient: str, page_size: int | None) -> tuple[str, dict[str, str]]:
    path, _, query = spec.query.partition("?")
    if spec.single:
        return f"{base}/{path}/{patient}", {}
    params = dict(parse_qsl(query, keep_blank_values=True))
    params["patient"] = patient
    if page_size:
        params["_count"] = str(page_size)
    return f"{base}/{path}", params


def error_text(r: requests.Response) -> str:
    """The status and the server's own explanation. Kept on the outcome (it may name the patient's
    records); only `sanitised` reaches the log."""
    try:
        body = r.json()
    except ValueError:
        return f"{r.status_code} {r.reason}"
    if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
        issue = (body.get("issue") or [{}])[0]
        text = issue.get("diagnostics") or (issue.get("details") or {}).get("text") or issue.get("code")
        return f"{r.status_code} {text}"
    return f"{r.status_code} {r.reason}"


def _next_link(body: Mapping[str, Any]) -> str | None:
    for link in body.get("link") or []:
        if link.get("relation") == "next":
            url = link.get("url")
            return url if isinstance(url, str) else None
    return None


def _pull(
    client: FhirClient,
    spec: QuerySpec,
    patient: str,
    spool: SpoolHandle,
    outcome: TypeOutcome,
    seen: set[str],
    save: Callable[[], None],
) -> None:
    url: str | None
    params: dict[str, str] | None
    url, params = search_url(client.base, spec, patient, client.profile.page_size)
    started = time.monotonic()
    while url:
        if outcome.pages >= client.profile.page_cap:
            outcome.capped = True
            break
        r = client.get(url, params)
        params = None  # `next` links carry their own query
        if r.status_code != 200:
            outcome.status, outcome.error = OutcomeStatus.ERROR, error_text(r)
            return
        body: JsonObject = r.json()
        outcome.pages += 1
        entries: list[JsonObject] = (
            [body] if spec.single else [e.get("resource") or {} for e in body.get("entry") or []]
        )
        for resource in entries:
            if resource.get("resourceType") != spec.resource_type:
                continue  # OperationOutcome warnings, _include'd resources
            rid = resource.get("id")
            if isinstance(rid, str):
                if rid in seen:
                    continue  # an Observation in both Labs and Vital Signs
                seen.add(rid)
            outcome.bytes += spool.append(spec.resource_type, resource)
            outcome.count += 1
        url = None if spec.single else _next_link(body)
        if outcome.pages % SAVE_EVERY_PAGES == 0:
            save()
    outcome.status = OutcomeStatus.OK
    outcome.seconds = round(time.monotonic() - started, 2)


def fetch_resources(
    job: Job[A],
    spool: SpoolHandle,
    settings: SourceSettings,
    client: FhirClient,
    save: Callable[[], None],
    log: JobLog,
) -> bool:
    """Fill the spool type by type. Returns False when the token expired part-way, so the
    documents pass is not attempted. Raises FetchAborted on a 401."""
    patient = job.source.ehr_patient_id
    specs = {q.label: q for q in settings.queries_for(job.source.scopes_granted)}
    by_type: dict[str, list[TypeOutcome]] = {}
    for o in job.outcomes:
        by_type.setdefault(o.resource_type, []).append(o)

    expired = False
    for resource_type, outcomes in by_type.items():
        if all(o.status.settled for o in outcomes):
            continue
        if expired:
            for o in outcomes:
                if not o.status.withheld:
                    o.status, o.error = OutcomeStatus.ERROR, "authorization expired before this was downloaded"
            continue
        spool.reset_type(resource_type)
        seen: set[str] = set()
        for o in outcomes:
            if o.status.withheld:
                continue
            o.reset()
            spec = specs.get(o.label)
            if spec is None:
                o.status, o.error = OutcomeStatus.SKIPPED, "scope not granted"
                continue
            try:
                _pull(client, spec, patient, spool, o, seen, save)
            except TokenExpired:
                o.status, o.error = OutcomeStatus.ERROR, "authorization expired during download"
                expired = True
                break
            except Unauthorized:
                raise FetchAborted("The health system stopped accepting our access. Please connect again.") from None
            except requests.RequestException as e:
                o.status, o.error = OutcomeStatus.ERROR, f"network: {type(e).__name__}"
            except ValueError:
                o.status, o.error = OutcomeStatus.ERROR, "unreadable response"
            if o.status is OutcomeStatus.ERROR:
                log.warning("%s: %s", o.label, sanitised(o.error), extra={"resource_type": resource_type})
            else:
                log.info(
                    "%s: %d resources in %d pages", o.label, o.count, o.pages, extra={"resource_type": resource_type}
                )
        spool.flush(resource_type)  # durable before the outcome that says so is saved
        save()
    return not expired
