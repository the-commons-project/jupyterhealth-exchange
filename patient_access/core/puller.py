"""Phase 4: walk the vendor's query table, page every search, append to the spool.

Per-type isolation: one row's failure is recorded on its outcome and the walk continues. A 401
means the token is gone, so the walk stops. Resume is per resource type: the spool is per type,
so a type is refetched from its first page unless every row for it settled.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl

import requests
from pydantic import BaseModel, ConfigDict, Field, SkipValidation, ValidationError, field_validator

from .client import FhirClient, RefusedUrl, TokenExpired, Unauthorized
from .interfaces import SpoolHandle
from .log import JobLog, sanitised
from .models import A, Job, JsonObject, OutcomeStatus, TypeOutcome
from .source_settings import QuerySpec, SourceSettings

SAVE_EVERY_PAGES = 5


class FetchAborted(Exception):
    """Phase 4 cannot continue (the token is gone). The message is safe to show the patient."""


def plan_outcomes(settings: SourceSettings, requested: tuple[str, ...], granted: tuple[str, ...]) -> list[TypeOutcome]:
    """One outcome per row of the query table, decided at the callback: `not_requested` when the
    patient left the type unchecked, `not_granted` when the EHR withheld a requested scope,
    `pending` for what the puller will fetch: requested *and* granted.

    The patient's choice wins over the EHR's grant. Epic grants every type in the app
    registration whatever was asked for, so a granted scope alone never means the patient chose
    to share that type."""
    asked = {q.label for q in settings.queries_for(requested)}
    fetchable = {q.label for q in settings.queries_for(granted)}

    def outcome(q: QuerySpec) -> TypeOutcome:
        status, error = (
            (OutcomeStatus.NOT_REQUESTED, "not selected by the patient")
            if q.label not in asked
            else (OutcomeStatus.NOT_GRANTED, "the organization did not grant access to this type")
            if q.label not in fetchable
            else (OutcomeStatus.PENDING, None)
        )
        return TypeOutcome(label=q.label, resource_type=q.resource_type, query=q.query, status=status, error=error)

    return [outcome(q) for q in settings.fetch.queries]


def _resource(value: Any) -> JsonObject:
    """The resource itself, uncopied, once it is known to be an object naming its type. Only the
    shape is checked: resources pass through as the EHR sent them, and copying each one here
    would double the work of the fetch's hottest loop."""
    if not (isinstance(value, dict) and isinstance(value.get("resourceType"), str)):
        raise ValueError("not a FHIR resource")
    return value


class _Link(BaseModel):
    model_config = ConfigDict(extra="ignore")

    relation: str
    url: str


class _Entry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resource: Annotated[JsonObject, SkipValidation] | None = None

    @field_validator("resource", mode="before")
    @classmethod
    def _object(cls, value: Any) -> JsonObject | None:
        return None if value is None else _resource(value)


class SearchPage(BaseModel):
    """The envelope of one search page: a Bundle whose entries are objects. The resources are
    passed through as parsed; only their shape is checked, never their content."""

    model_config = ConfigDict(extra="ignore")

    resource_type: Literal["Bundle"] = Field(alias="resourceType")
    entry: list[_Entry] = Field(default_factory=list)
    link: list[_Link] = Field(default_factory=list)

    @property
    def resources(self) -> list[JsonObject]:
        return [e.resource for e in self.entry if e.resource is not None]

    @property
    def next_url(self) -> str | None:
        return next((link.url for link in self.link if link.relation == "next"), None)


class UnreadablePage(Exception):
    """A 200 whose body is not the shape a FHIR search or read returns."""


def search_url(
    base: str, spec: QuerySpec, patient: str, page_size: int | None, route: str | None = None
) -> tuple[str, dict[str, str]]:
    path, _, query = (route or spec.query).partition("?")
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


def _json(r: requests.Response) -> tuple[Any, int]:
    """The body, with every NaN, Infinity and -Infinity (not JSON, but lenient servers send them
    and Python's parser accepts them) replaced by null; and how many there were. FHIR allows none
    of them; null keeps the hole visible rather than dropping the element."""
    replaced = 0

    def constant(name: str) -> None:
        nonlocal replaced
        replaced += 1

    return json.loads(r.content, parse_constant=constant), replaced


def _page(body: Any, single: bool) -> tuple[list[JsonObject], str | None]:
    """The resources a response holds and the next page's URL. Raises UnreadablePage."""
    try:
        if single:
            return [_resource(body)], None
        page = SearchPage.model_validate(body)
    except ValidationError:
        raise UnreadablePage() from None
    return page.resources, page.next_url


XML_TYPES = frozenset({"application/xml", "text/xml"})
CCDA_SUMMARY_TYPE = "34133-9"  # LOINC: summary of episode note (the patient summary)


def is_ccda(resource: JsonObject) -> bool:
    """A DocumentReference for a C-CDA, by what it says of itself, not by who sent it: the
    C-CDA format code, an XML attachment, or the patient-summary type. A server that ignores a
    filter it does not know answers with every document; this is what tells those apart."""
    doc_type = resource.get("type")
    codings = doc_type.get("coding") if isinstance(doc_type, dict) else None
    if any(isinstance(c, dict) and c.get("code") == CCDA_SUMMARY_TYPE for c in codings or []):
        return True
    for content in resource.get("content") or []:
        if not isinstance(content, dict):
            continue
        fmt, attachment = content.get("format"), content.get("attachment")
        if isinstance(fmt, dict) and "ccda" in str(fmt.get("code") or "").lower():
            return True
        if isinstance(attachment, dict):
            if str(attachment.get("contentType") or "").split(";")[0].strip().lower() in XML_TYPES:
                return True
    return False


KEEPERS: dict[str, Callable[[JsonObject], bool]] = {"ccda": is_ccda}


def _server_notes(resource: JsonObject) -> list[str]:
    """What an OperationOutcome among a page's entries says (why a result is empty or partial)."""
    out = []
    for issue in resource.get("issue") or []:
        if isinstance(issue, dict):
            details = issue.get("details")
            text = (details.get("text") if isinstance(details, dict) else None) or issue.get("diagnostics")
            out.append(str(text or issue.get("code") or "unspecified"))
    return out


def _pull(
    client: FhirClient,
    spec: QuerySpec,
    patient: str,
    spool: SpoolHandle,
    outcome: TypeOutcome,
    seen: set[str],
    save: Callable[[], None],
) -> int:
    """Pages one row into the spool. Returns how many non-finite numbers were replaced.

    A row with alternatives is asked for one route at a time, in order, until a route answers
    with something the row keeps. A route that errors, or answers with nothing kept, is noted on
    the outcome and passed over: these are other spellings of one request, and a server not
    knowing one of them is not a failure of the pull. When none answers the row is `ok` with
    nothing in it, and `attempts` says what each route said."""
    if not spec.alternatives:
        replaced, _ = _pull_route(client, spec, spec.query, patient, spool, outcome, seen, save, None)
        return replaced
    started = time.monotonic()
    replaced = 0
    outcome.query = spec.query  # a rerun starts over: the route an earlier run settled on is not kept
    for route in spec.routes:
        trial = TypeOutcome(label=outcome.label, resource_type=outcome.resource_type, query=route)
        notes: list[str] = []
        try:
            n, kept = _pull_route(client, spec, route, patient, spool, trial, seen, save, notes)
        except (TokenExpired, Unauthorized):
            raise
        except RefusedUrl as e:
            outcome.attempts.append(f"{route}: the server's next page was refused: {e.reason.value}")
            continue
        except requests.RequestException as e:
            outcome.attempts.append(f"{route}: network: {type(e).__name__}")
            continue
        except (UnreadablePage, ValueError):
            outcome.attempts.append(f"{route}: unreadable response")
            continue
        replaced += n
        if kept:
            outcome.query, outcome.status, outcome.error = route, trial.status, trial.error
            outcome.count, outcome.pages, outcome.bytes, outcome.capped = (
                trial.count,
                trial.pages,
                trial.bytes,
                trial.capped,
            )
            break
        said = trial.error if trial.status is OutcomeStatus.ERROR else "nothing kept"
        outcome.attempts.append(f"{route}: {said}" + (f" (server: {'; '.join(notes)})" if notes else ""))
    else:
        outcome.status = OutcomeStatus.OK
    outcome.seconds = round(time.monotonic() - started, 2)
    return replaced


def _pull_route(
    client: FhirClient,
    spec: QuerySpec,
    route: str,
    patient: str,
    spool: SpoolHandle,
    outcome: TypeOutcome,
    seen: set[str],
    save: Callable[[], None],
    notes: list[str] | None,
) -> tuple[int, int]:
    """Pages one route of a row into the spool. Returns how many non-finite numbers were replaced
    and how many resources the row keeps were seen (stored now or already in the spool)."""
    keep = KEEPERS[spec.keep] if spec.keep else None
    replaced = kept = 0
    url: str | None
    params: dict[str, str] | None
    url, params = search_url(client.base, spec, patient, client.profile.page_size, route)
    started = time.monotonic()
    while url:
        if outcome.pages >= client.profile.page_cap:
            outcome.capped = True
            break
        r = client.get(url, params)
        params = None  # `next` links carry their own query
        if r.status_code != 200:
            outcome.status, outcome.error = OutcomeStatus.ERROR, error_text(r)
            return replaced, kept
        body, nonfinite = _json(r)
        replaced += nonfinite
        resources, url = _page(body, spec.single)
        outcome.pages += 1
        for resource in resources:
            if resource.get("resourceType") != spec.resource_type:
                if notes is not None and resource.get("resourceType") == "OperationOutcome":
                    notes += _server_notes(resource)
                continue  # OperationOutcome warnings, _include'd resources
            if keep is not None and not keep(resource):
                continue  # the server answered with more than the row asks for
            kept += 1
            rid = resource.get("id")
            if isinstance(rid, str):
                if rid in seen:
                    continue  # an Observation in both Labs and Vital Signs
                seen.add(rid)
            outcome.bytes += spool.append(spec.resource_type, resource)
            outcome.count += 1
        if keep is not None and not kept:
            # A first page with nothing the row keeps: the server did not understand the route
            # (it ignored the filter and is sending every document). Its later pages are the
            # same answer, and a large record has hundreds of them.
            break
        if outcome.pages % SAVE_EVERY_PAGES == 0:
            save()
    outcome.status = OutcomeStatus.OK
    outcome.seconds = round(time.monotonic() - started, 2)
    return replaced, kept


def fetch_resources(
    job: Job[A],
    spool: SpoolHandle,
    settings: SourceSettings,
    client: FhirClient,
    save: Callable[[], None],
    log: JobLog,
) -> bool:
    """Fill the spool type by type. Returns False when the token expired part-way: the pipeline
    then fails the job rather than deliver part of the record. Raises FetchAborted on a 401."""
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
        # The type's spool is refetched from its first page, so every row of the type starts
        # again: an interrupted type can only read as an error, never as an earlier run's "ok".
        spool.reset_type(resource_type)
        for o in outcomes:
            if not o.status.withheld:
                o.reset()
        seen: set[str] = set()
        for o in outcomes:
            if o.status.withheld:
                continue
            spec = specs.get(o.label)
            if spec is None:
                o.status, o.error = OutcomeStatus.SKIPPED, "scope not granted"
                continue
            try:
                if replaced := _pull(client, spec, patient, spool, o, seen, save):
                    log.warning(
                        "%s: %d non-finite numbers replaced with null",
                        o.label,
                        replaced,
                        extra={"resource_type": resource_type},
                    )
            except TokenExpired:
                o.status, o.error = OutcomeStatus.ERROR, "authorization expired during download"
                expired = True
                break
            except Unauthorized:
                raise FetchAborted("The health system stopped accepting our access. Please connect again.") from None
            except RefusedUrl as e:
                o.status, o.error = OutcomeStatus.ERROR, f"the server's next page was refused: {e.reason.value}"
            except requests.RequestException as e:
                o.status, o.error = OutcomeStatus.ERROR, f"network: {type(e).__name__}"
            except (UnreadablePage, ValueError):
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
