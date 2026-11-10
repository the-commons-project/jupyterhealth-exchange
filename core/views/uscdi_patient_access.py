"""USCDI Patient Access: a patient-facing client that pulls the patient's records from their
hospital's patient portal server-side and delivers them to the deployment's S3 bucket, through
the patient_access library. The views here are the web half of a pull (the library's phases 1-3:
open, pick, authorize); the fetch and delivery run in a worker (phases 4-5).

Routes of this module:

    GET  /clients/uscdi-patient-access/?code=...   connect: the page (the shell redeems the link)
    POST /api/v1/uscdi-patient-access/launch       launch: the connect step -> {authorizeUrl}, and
                                                   this browser's callback cookie
    GET  /clients/uscdi-patient-access/callback    callback: the EHR's return; the job is created
                                                   and started
    GET  /api/v1/uscdi-patient-access/jobs         jobs: the patient's own pulls (hub, manage)
    GET  /api/v1/uscdi-patient-access/jobs/<id>    job: one pull (status, done)
    POST /api/v1/uscdi-patient-access/sign-out     sign_out: revokes the patient's token

The flow, in order. "shared" marks JHE routes every patient client uses, unchanged by this one;
the right-hand column is what the view calls in the library (`patient_access.core.flow`) or
what happens beyond JHE.

    browser (shell + client-uscdi-patient-access.js)   JHE view            library / EHR / worker
    ------------------------------------------------------------------------------------------
     1 GET  page?code=<invitation>                     connect             renders the shell
     2 POST api/v1/invitation/<token>                  shared              -> a grant
     3 POST /o/token/ (PKCE)                           shared (DOT)        -> bearer token, kept in
                                                                           sessionStorage; every
                                                                           call below sends it
     4 GET  users/profile, patients/<id>/consents,     shared              hub; the consent step
            fhir_sources                                                   POSTs/PATCHes consents
     5 GET  api/v1/ehr-patient-portal/brands?q=        shared              the picker; then the
                                                                           checklist, unless the
                                                                           vendor's own screen asks
     6 POST launch {location_id, data_source_id,       launch              start_upstream_auth:
            scopes}                                                        discovery, PKCE, the
                                                                           attempt stored. Answers
                                                                           {authorizeUrl}; sets the
                                                                           callback cookie
     7 location.assign(authorizeUrl)                                       EHR: the patient signs in
                                                                           and grants
     8 EHR redirects: GET callback?state&code          callback            begin_callback (consume
       (the cookie rides along)                                            the attempt); the cookie
                                                                           check; exchange_code (the
                                                                           EHR's token endpoint);
                                                                           _finish: FhirSource,
                                                                           patient identifier,
                                                                           create_job -> dispatched
                                                                           to a thread; 302 to
                                                                           page?route=status&job=
     9 GET  jobs/<id>, polled by the status screen     job                 meanwhile the worker runs
                                                                           pipeline.run_job: fetch
                                                                           every granted type and
                                                                           the documents into the
                                                                           spool, render the Bundle,
                                                                           deliver to S3. The cron
                                                                           sweep (every minute,
                                                                           patient_access_run_jobs)
                                                                           picks up anything a
                                                                           thread dropped
    10 done, manage: GET jobs?source=<data source>     jobs                the receipt; every pull
                                                                           of the patient's
    11 POST sign-out                                   sign_out            refresh token revoked
                                                                           (and the access token);
                                                                           the tab forgets it

Authentication is as for every other JHE client: a bearer token from the invitation, on every
API route here. The callback is the one route a token cannot reach, because it is the EHR
redirecting the browser. So the launch sets a cookie of its own in the browser that launched:
one per attempt, named from the state, a random value whose SHA-256 the attempt keeps, sent
only to the callback's path (`CallbackCookie`). The callback acts only for a browser that
presents it, so an authorization code delivered to another browser is never exchanged, and the
attempt is consumed before the check, so a lured callback spends the attacker's attempt. The
callback never reads `request.user`: the patient is the attempt's.

Errors reach the page as `?route=error&reason=<code>` from a closed set (`Reason`), never as
text; the client words each one. A refused launch answers `{"reason": <code>}` with 400, or 403
(not a patient) or 503 (module off). Nothing here touches the EHR Patient Portal client; the
picker and checklist are copies.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from oauth2_provider.models import AbstractApplication, get_application_model, get_refresh_token_model
from pydantic import BaseModel, ConfigDict, ValidationError
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from core.models import ClientDataSource, DataSource, EhrBrandLocation, EhrVendor, FhirSource, Patient
from core.patient_access.consent import consenting_study_ids
from core.patient_access.directory import JheOrmDirectory
from core.patient_access.identity import JheAttrs, LaunchAttrs, launch_participant, pull_participant
from core.patient_access.services import IdentifierConflict, attach_patient_identifier
from core.views.patient_facing import patient_facing_config
from patient_access.core.flow import (
    FlowError,
    begin_callback,
    create_job,
    exchange_code,
    start_upstream_auth,
)
from patient_access.core.models import AuthAttempt, Job, JobStatus, OutcomeStatus, TokenResponse
from patient_access.core.smart import ATTEMPT_TTL
from patient_access.core.vocab import FailureKind, TypeChoice
from patient_access.django_app.wiring import Module, NotConfigured, get_module

logger = logging.getLogger(__name__)

CLIENT_NAME = "USCDI Patient Access"
CLIENT_KEY = "uscdi-patient-access"
ERROR_ROUTE = "error"
CALLBACK_COOKIE_PREFIX = "pa_callback_"
# A minute past the attempt's own life, so a live attempt never meets an expired cookie: a slow
# sign-in is reported as expired (attempt_invalid), not as another browser.
CALLBACK_COOKIE_SECONDS = int(ATTEMPT_TTL.total_seconds()) + 60
NO_STORE = {"Cache-Control": "no-store"}


class Reason(StrEnum):
    """Why a launch or a callback stopped: all the page is told. The client words each one, so
    nothing from the EHR, and no organization's name, reaches the URL."""

    # The library's FlowErrorKind, one for one.
    UNKNOWN_SELECTION = "unknown_selection"
    NOT_CONFIGURED = "not_configured"
    SIGN_IN_UNAVAILABLE = "sign_in_unavailable"
    ATTEMPT_INVALID = "attempt_invalid"
    DECLINED = "declined"
    NO_CODE = "no_code"
    # This host's own.
    NOT_AVAILABLE = "not_available"  # the module is off
    NOT_A_PATIENT = "not_a_patient"  # the token's user has no patient record
    INVALID_REQUEST = "invalid_request"  # the launch's body did not parse
    SOURCE_UNAVAILABLE = "source_unavailable"  # the data source is not this client's
    CONSENT_REQUIRED = "consent_required"  # launch: the patient has not consented to share it
    NO_TYPES = "no_types"  # nothing ticked on the checklist
    BROWSER_MISMATCH = "browser_mismatch"  # the callback reached a browser without the attempt's cookie
    CONSENT_WITHDRAWN = "consent_withdrawn"  # callback: consent withdrawn since the launch
    IDENTIFIER_CONFLICT = "identifier_conflict"  # the EHR patient is another JHE patient's


class ConsentWithdrawn(Exception):
    """The patient stopped sharing with the study between the launch and the callback."""


def _client() -> AbstractApplication | None:
    """This client's OAuth application row (the invitation names it), or None."""
    return get_application_model().objects.filter(name=CLIENT_NAME).first()


def _page() -> str:
    return reverse("uscdi-patient-access-connect")


def _redirect_uri(request: HttpRequest) -> str:
    return request.build_absolute_uri(reverse("uscdi-patient-access-callback"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _patient(request: Request) -> Patient | None:
    """The token's user's patient record, or None for a user without one."""
    return request.user.get_patient()


class CallbackCookie(BaseModel):
    """The cookie that binds an attempt's callback to the browser that launched it. Named per
    attempt (from its state), so a second launch before the first callback leaves the first's in
    place. Its path is the redirect URI's, so it is sent to the callback and nowhere else, and it
    is set on the response alone: no global cookie setting changes."""

    model_config = ConfigDict(frozen=True)

    name: str
    path: str
    secure: bool

    @classmethod
    def for_attempt(cls, request: HttpRequest, state: str) -> CallbackCookie:
        return cls(
            name=f"{CALLBACK_COOKIE_PREFIX}{_sha256(state)[:16]}",
            path=urlparse(_redirect_uri(request)).path,
            # A cookie only an https page may send, unless this is development over plain http.
            secure=request.is_secure() or not settings.DEBUG,
        )

    def set(self, response: HttpResponse, value: str) -> None:
        response.set_cookie(
            self.name,
            value,
            max_age=CALLBACK_COOKIE_SECONDS,
            path=self.path,
            secure=self.secure,
            httponly=True,
            samesite="Lax",  # sent on the EHR's top-level redirect back; Strict would drop it
        )

    def clear(self, response: HttpResponse) -> None:
        response.set_cookie(self.name, "", max_age=0, path=self.path, secure=self.secure, httponly=True, samesite="Lax")


def _config() -> dict[str, Any]:
    config = patient_facing_config(CLIENT_NAME, CLIENT_KEY, _page())
    config["expectedResourceTypes"] = sorted(
        {s.removeprefix("patient/").removesuffix(".read") for s in EhrVendor.SUPPORTED_SCOPES}
    )
    # Vendors whose own screen asks per type: the client skips its checklist for them.
    config["typeChoiceByVendor"] = JheOrmDirectory.type_choices()
    try:
        module: Module | None = get_module()
    except NotConfigured:
        module = None
    config["available"] = module is not None
    # The client follows an authorize URL only over https, or http where the module allows it
    # (development).
    config["allowHttpSignIn"] = module is not None and module.config.allow_http
    return config


def connect(request: HttpRequest) -> HttpResponse:
    """The page. The shell redeems the invitation link's ?code= itself, at JHE's invitation
    endpoint, as for the EHR Patient Portal."""
    return render(request, "clients/uscdi-patient-access/connect.html", {"config": _config()})


def _refused(reason: Reason, status: int) -> Response:
    return Response({"reason": reason.value}, status=status, headers=NO_STORE)


class LaunchRequest(BaseModel):
    """The connect step's choice: the facility picked, the study connection, and the ticked
    record types (absent for a vendor whose own screen asks per type)."""

    model_config = ConfigDict(frozen=True, extra="ignore", coerce_numbers_to_str=True)

    location_id: str
    data_source_id: int
    scopes: tuple[str, ...] = ()


@api_view(["POST"])
def launch(request: Request) -> Response:
    """The connect step's submit. Discovery and PKCE run here and the attempt is recorded; the
    answer is the EHR's authorize URL, and this browser's callback cookie is set on it."""
    patient = _patient(request)
    if patient is None:
        return _refused(Reason.NOT_A_PATIENT, 403)
    try:
        module = get_module()
    except NotConfigured:
        return _refused(Reason.NOT_AVAILABLE, 503)
    try:
        choice = LaunchRequest.model_validate(request.data)
    except ValidationError:
        return _refused(Reason.INVALID_REQUEST, 400)
    data_source = DataSource.objects.filter(id=choice.data_source_id).first()
    if data_source is None or not ClientDataSource.objects.filter(client=_client(), data_source=data_source).exists():
        return _refused(Reason.SOURCE_UNAVAILABLE, 400)
    if not consenting_study_ids(patient, data_source):
        return _refused(Reason.CONSENT_REQUIRED, 400)
    try:
        selection = module.directory.selection(choice.location_id)
    except KeyError:
        return _refused(Reason.UNKNOWN_SELECTION, 400)
    offered = JheOrmDirectory.offered_scopes(selection)
    if JheOrmDirectory.type_choice(selection.endpoint.vendor) is TypeChoice.EHR:
        chosen = offered  # the vendor's screen asks; our checklist was skipped
    else:
        chosen = tuple(s for s in offered if s in choice.scopes)
        if not chosen:
            return _refused(Reason.NO_TYPES, 400)
    cookie_value = secrets.token_urlsafe(32)
    try:
        url = start_upstream_auth(
            participant=launch_participant(patient, data_source.id, _sha256(cookie_value)),
            selection_id=choice.location_id,
            scopes=chosen,
            scopes_declined=tuple(s for s in offered if s not in chosen),
            redirect_uri=_redirect_uri(request),
            directory=module.directory,
            keyring=module.keyring,
            attempts=module.attempts,
            http=module.http,
            clock=module.clock,
            allow_http=module.config.allow_http,
        )
    except FlowError as e:
        logger.warning("uscdi-patient-access launch refused: %s", e.kind.value)
        return _refused(Reason(e.kind.value), 400)
    state = parse_qs(urlparse(url).query)["state"][0]
    response = Response({"authorize_url": url}, headers=NO_STORE)
    CallbackCookie.for_attempt(request, state).set(response, cookie_value)
    return response


def _finish(module: Module, patient: Patient, attempt: AuthAttempt[LaunchAttrs], token: TokenResponse) -> Job[JheAttrs]:
    """JHE's records for the pull and the job, in one transaction; the thread starts on commit."""
    data_source = DataSource.objects.get(id=attempt.participant.attrs.data_source_id)
    study_ids = consenting_study_ids(patient, data_source)
    if not study_ids:
        raise ConsentWithdrawn()
    location = (
        EhrBrandLocation.objects.select_related("brand__vendor").filter(id=int(attempt.selection.id)).first()
        if attempt.selection.id.isdigit()
        else None
    )
    with transaction.atomic():
        fhir_source, _ = FhirSource.objects.get_or_create_for_pull(patient, data_source, location)
        attach_patient_identifier(patient, attempt.selection.endpoint.iss, token.patient)
        return create_job(
            attempt,
            token,
            pull_participant(patient, data_source, fhir_source, study_ids),
            directory=module.directory,
            jobs=module.jobs,
            dispatcher=module.dispatcher,
            clock=module.clock,
        )


def _fail(reason: Reason) -> HttpResponse:
    return redirect(f"{_page()}?{urlencode({'route': ERROR_ROUTE, 'reason': reason.value})}")


def _callback(request: HttpRequest, state: str, cookie: CallbackCookie) -> HttpResponse:
    try:
        module = get_module()
    except NotConfigured:
        return _fail(Reason.NOT_AVAILABLE)
    try:
        attempt: AuthAttempt[LaunchAttrs] = begin_callback(state, module.attempts)
    except FlowError as e:
        logger.warning("uscdi-patient-access callback refused: %s", e.kind.value)
        return _fail(Reason(e.kind.value))
    presented = request.COOKIES.get(cookie.name)
    if presented is None:
        logger.warning("uscdi-patient-access callback refused: this browser has no callback cookie for the attempt")
        return _fail(Reason.BROWSER_MISMATCH)
    if not hmac.compare_digest(_sha256(presented), attempt.participant.attrs.callback_cookie_sha256):
        logger.warning("uscdi-patient-access callback refused: the callback cookie is not the attempt's")
        return _fail(Reason.BROWSER_MISMATCH)
    patient = Patient.objects.filter(id=attempt.participant.attrs.patient_id).first()
    if patient is None:
        return _fail(Reason.ATTEMPT_INVALID)
    try:
        token = exchange_code(
            attempt,
            code=request.GET.get("code"),
            error=request.GET.get("error"),
            directory=module.directory,
            keyring=module.keyring,
            http=module.http,
            clock=module.clock,
        )
        job = _finish(module, patient, attempt, token)
    except FlowError as e:
        logger.warning("uscdi-patient-access callback failed: %s", e.kind.value)
        return _fail(Reason(e.kind.value))
    except ConsentWithdrawn:
        logger.warning("uscdi-patient-access callback failed: consent withdrawn since the launch")
        return _fail(Reason.CONSENT_WITHDRAWN)
    except IdentifierConflict:
        logger.warning("uscdi-patient-access callback failed: identifier conflict")
        return _fail(Reason.IDENTIFIER_CONFLICT)
    query = {"route": "status", "job": job.id, "source": str(job.participant.attrs.data_source_id)}
    return redirect(f"{_page()}?{urlencode(query)}")


@never_cache
@require_GET
def callback(request: HttpRequest) -> HttpResponse:
    """The EHR's return. In order: consume the attempt (single use, committed at once); check this
    browser holds the attempt's callback cookie (else stop, the code unspent); exchange the code;
    record the pull and create the job. The cookie is cleared whatever the outcome. The patient is
    the attempt's: `request.user` is never read, so a practitioner's login in the same browser
    plays no part."""
    state = request.GET.get("state", "")
    cookie = CallbackCookie.for_attempt(request, state)
    response = _callback(request, state, cookie)
    cookie.clear(response)
    return response


class TypeView(BaseModel):
    label: str
    resource_type: str
    status: OutcomeStatus
    count: int


class SourceView(BaseModel):
    brand: str
    location: str | None


class DocumentsView(BaseModel):
    status: OutcomeStatus
    stored: int
    skipped: int


class JobView(BaseModel):
    """What the client's screens show of a pull. `failure_kind` decides the patient's words (the
    client maps each kind to its copy); `failure` is the operator's text and is not shown."""

    id: str
    status: JobStatus
    terminal: bool
    data_source_id: int
    fhir_source_id: int  # the hospital's record in JHE: the client groups a patient's pulls by it
    finished_at: datetime | None
    source: SourceView
    types: list[TypeView]
    documents: DocumentsView
    delivered: bool
    failure_kind: FailureKind | None

    @classmethod
    def of(cls, job: Job[JheAttrs]) -> JobView:
        docs = job.documents
        return cls(
            id=job.id,
            status=job.status,
            terminal=job.status.terminal,
            data_source_id=job.participant.attrs.data_source_id,
            fhir_source_id=job.participant.attrs.fhir_source_id,
            finished_at=job.finished_at,
            source=SourceView(brand=job.source.brand_name, location=job.source.facility_name),
            types=[
                TypeView(label=o.label, resource_type=o.resource_type, status=o.status, count=o.count)
                for o in job.outcomes
            ],
            documents=DocumentsView(status=docs.status, stored=len(docs.stored), skipped=sum(docs.skipped.values())),
            delivered=job.delivery.receipt is not None,
            failure_kind=job.failure_kind if job.status in (JobStatus.FAILED, JobStatus.FAILED_TERMINAL) else None,
        )


@api_view(["GET"])
def jobs(request: Request) -> Response:
    """The patient's own pulls, newest first; `?source=` narrows to one data source."""
    patient = _patient(request)
    source = request.query_params.get("source")
    try:
        narrowed = {"data_source_id": int(source)} if source else None
    except ValueError:
        return Response({"error": "source must be a number"}, status=400)
    try:
        module = get_module()
    except NotConfigured:
        return Response({"results": []})
    found = module.jobs.for_participant(str(patient.id), narrowed) if patient is not None else []
    return Response({"results": [JobView.of(j).model_dump(mode="json") for j in found]})


@api_view(["GET"])
def job(request: Request, job_id: str) -> Response:
    patient = _patient(request)
    try:
        found = get_module().jobs.get(job_id) if patient is not None else None
    except (
        NotConfigured,
        KeyError,
        ValidationError,
    ):  # ValidationError: a row the crash cap ended with nothing to read
        found = None
    if found is None or patient is None or found.participant.ref != str(patient.id):
        return Response({"error": "Not found"}, status=404)
    return Response(JobView.of(found).model_dump(mode="json"))


@api_view(["POST"])
def sign_out(request: Request) -> Response:
    """Revokes the token the request carries and the refresh token issued with it. Revoking a
    refresh token revokes its access token too, while revoking only the access token would leave
    the refresh token, which never expires, alive. The shell never stores the refresh token, so it
    is found here from the access token."""
    access = request.auth
    if access is not None:
        refresh = get_refresh_token_model().objects.filter(access_token=access).first()
        (refresh if refresh is not None else access).revoke()
    return Response(status=204)
