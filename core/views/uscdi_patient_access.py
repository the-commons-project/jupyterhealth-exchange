"""USCDI Patient Access: a patient-facing client that pulls the patient's records from their
hospital's patient portal server-side and delivers them to the deployment's S3 bucket, through
the patient_access library.

    GET  /clients/uscdi-patient-access/?code=...     invitation -> patient session -> the shell
    POST /clients/uscdi-patient-access/launch        the connect form -> 302 to the EHR's authorize URL
    GET  /clients/uscdi-patient-access/callback      the EHR's return: the job is created and started
    POST /clients/uscdi-patient-access/sign-out      ends the session
    GET  /api/v1/uscdi-patient-access/jobs           the patient's own pulls, for the hub and manage screens
    GET  /api/v1/uscdi-patient-access/jobs/<id>      the patient's own job, for the status screen
    GET  /api/v1/uscdi-patient-access/profile        the shell's calls, on this client's own routes:
    GET, POST, PATCH .../consents                    each delegates to JHE's existing view for the
    GET  /api/v1/uscdi-patient-access/fhir-sources   session's own patient, so the logic stays in one
    GET  /api/v1/uscdi-patient-access/brands         place and JHE's shared routes stay token-only

The patient is authenticated by a Django session the invitation link starts and marks as this
client's (`core.patient_access.authentication`), never by the shell's bearer token. Only a
patient-only user whose session carries the mark for their own patient counts as "the patient"
anywhere here. The callback acts only when that patient is the one who launched, so an
authorization code delivered to another browser is never exchanged. Nothing here touches the EHR
Patient Portal client; the picker and checklist are copies.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from django.contrib.auth import login, logout
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from oauth2_provider.models import AbstractApplication, get_application_model
from pydantic import BaseModel, ConfigDict, ValidationError
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from core.models import (
    ClientDataSource,
    DataSource,
    EhrBrandLocation,
    EhrVendor,
    FhirSource,
    Patient,
    PatientInvitation,
)
from core.models.patient_invitation import (
    InvitationCancelled,
    InvitationConflict,
    InvitationExpired,
    InvitationForAnotherClient,
    InvitationWithoutUser,
)
from core.patient_access.authentication import (
    InvitationSessionAuthentication,
    is_patient,
    mark_session,
    session_mark,
)
from core.patient_access.consent import consenting_study_ids
from core.patient_access.directory import JheOrmDirectory
from core.patient_access.identity import JheAttrs, LaunchAttrs, launch_participant, pull_participant
from core.patient_access.services import IdentifierConflict, attach_patient_identifier
from core.views.ehr_patient_portal import brands_search
from core.views.fhir_source import FhirSourceViewSet
from core.views.jhe_user import JheUserViewSet
from core.views.patient import PatientViewSet
from core.views.patient_facing import patient_facing_config
from patient_access.core.flow import (
    FlowError,
    FlowErrorKind,
    begin_callback,
    create_job,
    exchange_code,
    start_upstream_auth,
)
from patient_access.core.models import AuthAttempt, Job, JobStatus, OutcomeStatus, TokenResponse
from patient_access.core.vocab import FailureKind, TypeChoice
from patient_access.django_app.wiring import Module, NotConfigured, get_module

logger = logging.getLogger(__name__)

CLIENT_NAME = "USCDI Patient Access"
CLIENT_KEY = "uscdi-patient-access"
# Long enough for any pull (the EHR token lives an hour) and the invitation's 12 h re-use window;
# far shorter than Django's two-week default.
SESSION_SECONDS = 12 * 3600
# The patient-readable error for the next page view. Carried in the session, never in the URL,
# so EHR-sourced text never reaches a proxy's access log.
FLOW_ERROR_KEY = "uscdi_patient_access_flow_error"
LOGIN_BACKEND = "django.contrib.auth.backends.ModelBackend"

ERROR_ROUTE = "error"


class FlowMessage(BaseModel):
    """A patient-readable message for the error screen, carried in the session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    message: str
    retry: bool = True  # False: trying again cannot help (the session is gone); only Back is offered


INVALID_INVITATION = FlowMessage(
    title="This invitation link isn't valid",
    message="It may have expired or been replaced. Ask your study team for a new link.",
    retry=False,
)
SESSION_ENDED = FlowMessage(
    title="Your session has ended", message="Open the link from your invitation email to continue.", retry=False
)
NOT_AVAILABLE = FlowMessage(
    title="Not available", message="Sharing records from your healthcare organization is not available here yet."
)
SIGNED_OUT = FlowMessage(
    title="You're signed out",
    message="You can close this page. To share again, open the link from your invitation email.",
    retry=False,
)
CONNECT_FAILED = "We couldn't connect to your healthcare organization"
LAUNCH_FAILED = "We couldn't start the connection"


def _client() -> AbstractApplication | None:
    """This client's OAuth application row (the invitation names it), or None."""
    return get_application_model().objects.filter(name=CLIENT_NAME).first()


def _page() -> str:
    return reverse("uscdi-patient-access-connect")


def _patient(request: HttpRequest | Request) -> Patient | None:
    """The patient this client's session is for: a user whose session carries the invitation
    link's mark for their own patient. Anyone else is nobody here."""
    user = request.user
    mark = session_mark(request)
    patient = user.get_patient() if mark is not None and is_patient(user) else None
    return patient if patient is not None and mark is not None and patient.id == mark.patient_id else None


def _fail(request: HttpRequest, flow: FlowMessage) -> HttpResponse:
    request.session[FLOW_ERROR_KEY] = flow.model_dump()
    return redirect(f"{_page()}?{urlencode({'route': ERROR_ROUTE})}")


def _flow_error(request: HttpRequest) -> FlowMessage | None:
    """The error a redirect carried to the error route. Kept while the patient is on that route (a
    reload shows it again); dropped, unshown, once they are anywhere else."""
    if request.GET.get("route") != ERROR_ROUTE:
        request.session.pop(FLOW_ERROR_KEY, None)
        return None
    try:
        raw = request.session.get(FLOW_ERROR_KEY)
        return FlowMessage.model_validate(raw) if raw is not None else None
    except ValidationError:
        return None


def _config(request: HttpRequest, flow: FlowMessage | None = None) -> dict[str, Any]:
    """`flow`: an error this very request found (the module off), shown on any route."""
    config = patient_facing_config(CLIENT_NAME, CLIENT_KEY, _page())
    config["expectedResourceTypes"] = sorted(
        {s.removeprefix("patient/").removesuffix(".read") for s in EhrVendor.SUPPORTED_SCOPES}
    )
    # The shell runs in session mode for this client: same-origin requests with the CSRF token,
    # no bearer token, no stored-token gate (core/static/common/js/patient-facing.js).
    config["auth"] = "session"
    config["csrfToken"] = get_token(request)
    config["launchUrl"] = reverse("uscdi-patient-access-launch")
    # Vendors whose own screen asks per type: the client skips its checklist for them.
    config["typeChoiceByVendor"] = JheOrmDirectory.type_choices()
    config["signOutUrl"] = reverse("uscdi-patient-access-sign-out")
    # The shell's own calls, pointed at this client's routes (see the module docstring).
    config["apiPaths"] = {
        "profile": "uscdi-patient-access/profile",
        "consents": "uscdi-patient-access/consents",
        "fhirSources": "uscdi-patient-access/fhir-sources",
    }
    flow = flow or _flow_error(request)
    if flow is None and _patient(request) is None:
        flow = SESSION_ENDED
    config["flowError"] = flow.model_dump() if flow is not None else None
    return config


def _token_from_code(code: str) -> str:
    """The invitation code is `{host}_{token}` (PatientInvitation.build_link)."""
    return code.rpartition("_")[2]


@ensure_csrf_cookie
def connect(request: HttpRequest) -> HttpResponse:
    """The invitation link: redeem the code for a patient session, strip it from the URL, then
    serve the patient-facing shell."""
    code = request.GET.get("code")
    if code:
        # Opening a link always starts from nothing: whoever this browser was signed in as (an
        # earlier patient on a shared device, a practitioner) is signed out first, so a link that
        # then fails leaves no session behind for the error screen's Back to walk into.
        logout(request)
        client = _client()
        if client is None:
            return _fail(request, INVALID_INVITATION)
        try:
            user = PatientInvitation.redeem_for_user(_token_from_code(code), client)
        except (
            PatientInvitation.DoesNotExist,
            InvitationExpired,
            InvitationCancelled,
            InvitationConflict,
            InvitationForAnotherClient,
            InvitationWithoutUser,
        ):
            return _fail(request, INVALID_INVITATION)
        # login() rotates the session key and replaces any session this browser had; the mark
        # is written after it, so it can only ever be on the session the invitation started.
        login(request, user, backend=LOGIN_BACKEND)
        mark_session(request, user.get_patient().id)
        request.session.set_expiry(SESSION_SECONDS)
        params = request.GET.copy()
        params.pop("code")
        return redirect(f"{_page()}?{params.urlencode()}" if params else _page())
    try:
        get_module()
        unavailable = None
    except NotConfigured:
        unavailable = NOT_AVAILABLE
    return render(request, "clients/uscdi-patient-access/connect.html", {"config": _config(request, unavailable)})


def _error(message: str, status: int) -> Response:
    return Response({"error": message}, status=status)


class LaunchForm(BaseModel):
    """The connect form, as posted: the facility picked, the study connection, and the ticked
    record types (absent for a vendor whose own screen asks per type)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    location_id: str
    data_source_id: int
    scopes: tuple[str, ...] = ()

    @classmethod
    def from_post(cls, post: QueryDict) -> LaunchForm:
        return cls.model_validate(
            {
                "location_id": post.get("location_id", ""),
                "data_source_id": post.get("data_source_id"),
                "scopes": post.getlist("scopes"),
            }
        )


@require_POST
def launch(request: HttpRequest) -> HttpResponse:
    """The connect form's submit. Discovery and PKCE run here, the attempt is recorded, and the
    browser is redirected to the EHR's authorize URL; the URL never passes through page script.
    CSRF is Django's middleware (the form carries the token). Errors go to the shell's error
    route with the message in the session, as the callback's do."""
    patient = _patient(request)
    if patient is None:
        return _fail(request, SESSION_ENDED)
    try:
        module = get_module()
    except NotConfigured:
        return _fail(request, NOT_AVAILABLE)
    try:
        form = LaunchForm.from_post(request.POST)
    except ValidationError:
        return _fail(
            request, FlowMessage(title=LAUNCH_FAILED, message="Please choose your healthcare organization again.")
        )
    data_source = DataSource.objects.filter(id=form.data_source_id).first()
    if data_source is None or not ClientDataSource.objects.filter(client=_client(), data_source=data_source).exists():
        return _fail(request, FlowMessage(title=LAUNCH_FAILED, message="This study connection is not available."))
    if not consenting_study_ids(patient, data_source):
        return _fail(
            request,
            FlowMessage(title=LAUNCH_FAILED, message="Please agree to share your records with your study first."),
        )
    try:
        selection = module.directory.selection(form.location_id)
    except KeyError:
        return _fail(request, FlowMessage(title=LAUNCH_FAILED, message="That organization is not in the directory."))
    offered = JheOrmDirectory.offered_scopes(selection)
    if JheOrmDirectory.type_choice(selection.endpoint.vendor) is TypeChoice.EHR:
        chosen = offered  # the vendor's screen asks; our checklist was skipped
    else:
        chosen = tuple(s for s in offered if s in form.scopes)
        if not chosen:
            return _fail(
                request, FlowMessage(title=LAUNCH_FAILED, message="Please choose at least one kind of record to share.")
            )
    try:
        url = start_upstream_auth(
            participant=launch_participant(patient, data_source.id),
            selection_id=form.location_id,
            scopes=chosen,
            scopes_declined=tuple(s for s in offered if s not in chosen),
            redirect_uri=request.build_absolute_uri(reverse("uscdi-patient-access-callback")),
            directory=module.directory,
            keyring=module.keyring,
            attempts=module.attempts,
            http=module.http,
            clock=module.clock,
            allow_http=module.config.allow_http,
        )
    except FlowError as e:
        return _fail(request, FlowMessage(title=LAUNCH_FAILED, message=str(e)))
    return redirect(url)


def _finish(module: Module, patient: Patient, attempt: AuthAttempt[LaunchAttrs], token: TokenResponse) -> Job[JheAttrs]:
    """JHE's records for the pull and the job, in one transaction; the thread starts on commit."""
    data_source = DataSource.objects.get(id=attempt.participant.attrs.data_source_id)
    study_ids = consenting_study_ids(patient, data_source)
    if not study_ids:
        raise FlowError(
            FlowErrorKind.DECLINED, "You are no longer sharing records with this study, so nothing was fetched."
        )
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


@require_GET
def callback(request: HttpRequest) -> HttpResponse:
    """The EHR's return. In order: consume the attempt (single use, committed at once); check the
    session's patient launched it (else stop, the code unspent); exchange the code; record the
    pull and create the job. Errors go to the shell's error route with the message in the session."""
    try:
        module = get_module()
    except NotConfigured:
        return _fail(request, NOT_AVAILABLE)
    try:
        attempt = begin_callback(request.GET.get("state", ""), module.attempts)
    except FlowError as e:
        return _fail(request, FlowMessage(title=CONNECT_FAILED, message=str(e)))
    patient = _patient(request)
    if patient is None or str(patient.id) != attempt.participant.ref:
        logger.warning("uscdi-patient-access callback refused: the session is not the launching patient's")
        return _fail(
            request,
            FlowMessage(
                title=CONNECT_FAILED,
                message="Please open your invitation link again on this device and connect from there.",
            ),
        )
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
    except (FlowError, IdentifierConflict) as e:
        return _fail(request, FlowMessage(title=CONNECT_FAILED, message=str(e)))
    query = {"route": "status", "job": job.id, "source": str(job.participant.attrs.data_source_id)}
    return redirect(f"{_page()}?{urlencode(query)}")


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
@authentication_classes([InvitationSessionAuthentication])
@permission_classes([IsAuthenticated])
def jobs(request: Request) -> Response:
    """The patient's own pulls, newest first; `?source=` narrows to one data source."""
    patient = _patient(request)
    source = request.query_params.get("source")
    try:
        narrowed = {"data_source_id": int(source)} if source else None
    except ValueError:
        return _error("source must be a number", 400)
    try:
        module = get_module()
    except NotConfigured:
        return Response({"results": []})
    found = module.jobs.for_participant(str(patient.id), narrowed) if patient is not None else []
    return Response({"results": [JobView.of(j).model_dump(mode="json") for j in found]})


@api_view(["GET"])
@authentication_classes([InvitationSessionAuthentication])
@permission_classes([IsAuthenticated])
def job(request: Request, job_id: str) -> Response:
    patient = _patient(request)
    try:
        found = get_module().jobs.get(job_id) if patient is not None else None
    except (NotConfigured, KeyError):
        found = None
    if found is None or patient is None or found.participant.ref != str(patient.id):
        return _error("Not found", 404)
    return Response(JobView.of(found).model_dump(mode="json"))


@require_POST
def sign_out(request: HttpRequest) -> HttpResponse:
    """Ends the session (Django's CSRF middleware checks the form). The 12-hour expiry stays the
    backstop for a patient who just closes the page."""
    logout(request)
    return _fail(request, SIGNED_OUT)


# --- the shell's calls, on this client's own routes ------------------------------------------
#
# JHE's existing views, bound to this client's session authentication instead of their tokens:
# the logic stays where it is, the shared routes stay token-only, and each call here can only
# ever act for the session's own patient.

_SESSION_ONLY: dict[str, Any] = {
    "authentication_classes": [InvitationSessionAuthentication],
    "permission_classes": [IsAuthenticated],
}
_profile_view = JheUserViewSet.as_view({"get": "profile"}, **_SESSION_ONLY)
_consents_view = PatientViewSet.as_view(
    {"get": "consents", "post": "consents", "patch": "consents"}, detail=True, **_SESSION_ONLY
)
_sources_view = FhirSourceViewSet.as_view({"get": "list"}, **_SESSION_ONLY)
_brands_view = brands_search.cls.as_view(**_SESSION_ONLY)
_FORBIDDEN_BODY = {"detail": "Authentication credentials were not provided."}


def profile(request: HttpRequest) -> HttpResponse:
    return _profile_view(request)


def consents(request: HttpRequest) -> HttpResponse:
    """The session's own patient's consents. Never the dev-and-test `reset` switch of the shared
    view: a GET that deletes would be triggerable by any link once a cookie authenticates it."""
    patient = _patient(request)
    if patient is None:
        return JsonResponse(_FORBIDDEN_BODY, status=403)
    if "reset" in request.GET:
        return JsonResponse({"detail": "Not available here."}, status=400)
    return _consents_view(request, pk=str(patient.id))


def fhir_sources(request: HttpRequest) -> HttpResponse:
    return _sources_view(request)


def brands(request: HttpRequest) -> HttpResponse:
    return _brands_view(request)
