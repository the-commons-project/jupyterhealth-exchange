"""USCDI Patient Access: a patient-facing client that pulls the patient's records from their
hospital's patient portal server-side and delivers them to the deployment's S3 bucket, through
the patient_access library.

    GET  /clients/uscdi-patient-access/?code=...     invitation -> patient session -> the shell
    POST /clients/uscdi-patient-access/launch        the connect form -> 302 to the EHR's authorize URL
    GET  /clients/uscdi-patient-access/callback      the EHR's return: the job is created and started
    GET  /api/v1/uscdi-patient-access/jobs           the patient's own pulls, for the hub and manage screens
    GET  /api/v1/uscdi-patient-access/jobs/<id>      the patient's own job, for the status screen

The patient is authenticated by a Django session the invitation link starts (not the shell's
bearer token). The callback acts only when that session's patient is the one who launched, so an
authorization code delivered to another browser is never exchanged. Nothing here touches the EHR
Patient Portal client; the picker and checklist are copies.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.contrib.auth import login
from django.db import transaction
from django.http import HttpRequest, HttpResponse, QueryDict
from django.middleware.csrf import get_token
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from oauth2_provider.models import get_application_model
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
    PatientInvitation,
)
from core.models.patient_invitation import InvitationCancelled, InvitationConflict, InvitationExpired
from core.patient_access.authentication import PatientSessionAuthentication
from core.patient_access.consent import consenting_study_ids
from core.patient_access.directory import JheOrmDirectory
from core.patient_access.identity import launch_participant, pull_participant
from core.patient_access.services import IdentifierConflict, attach_patient_identifier
from core.views.patient_facing import patient_facing_config
from patient_access.core.flow import (
    FlowError,
    FlowErrorKind,
    begin_callback,
    create_job,
    exchange_code,
    start_upstream_auth,
)
from patient_access.core.models import Job
from patient_access.core.vendors import TypeChoice
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

INVALID_INVITATION = (
    "This invitation link isn't valid",
    "It may have expired or been replaced. Ask your study team for a new link.",
)
SESSION_ENDED = ("Your session has ended", "Open the link from your invitation email to continue.")
NOT_AVAILABLE = ("Not available", "Sharing records from your healthcare organization is not available here yet.")
CONNECT_FAILED = "We couldn't connect to your healthcare organization"
LAUNCH_FAILED = "We couldn't start the connection"


def _client():
    return get_application_model().objects.filter(name=CLIENT_NAME).first()


def _page() -> str:
    return reverse("uscdi-patient-access-connect")


def _patient(request: HttpRequest):
    user = request.user
    return user.get_patient() if user.is_authenticated and user.is_patient() else None


def _fail(request: HttpRequest, title: str, message: str) -> HttpResponse:
    request.session[FLOW_ERROR_KEY] = {"title": title, "message": message}
    return redirect(f"{_page()}?{urlencode({'route': 'error'})}")


def _config(request: HttpRequest) -> dict:
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
    config["flowError"] = request.session.pop(FLOW_ERROR_KEY, None)
    if config["flowError"] is None and _patient(request) is None:
        config["flowError"] = dict(zip(("title", "message"), SESSION_ENDED, strict=True))
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
        client = _client()
        try:
            if client is None:
                raise InvitationConflict()
            user = PatientInvitation.redeem_for_user(_token_from_code(code), client)
        except (
            PatientInvitation.DoesNotExist,
            InvitationExpired,
            InvitationCancelled,
            InvitationConflict,
            ValueError,
        ):
            return _fail(request, *INVALID_INVITATION)
        login(request, user, backend=LOGIN_BACKEND)
        request.session.set_expiry(SESSION_SECONDS)
        params = request.GET.copy()
        params.pop("code")
        return redirect(f"{_page()}?{params.urlencode()}" if params else _page())
    try:
        get_module()
    except NotConfigured:
        request.session[FLOW_ERROR_KEY] = dict(zip(("title", "message"), NOT_AVAILABLE, strict=True))
    return render(request, "clients/uscdi-patient-access/connect.html", {"config": _config(request)})


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
        return _fail(request, *SESSION_ENDED)
    try:
        module = get_module()
    except NotConfigured:
        return _fail(request, *NOT_AVAILABLE)
    try:
        form = LaunchForm.from_post(request.POST)
    except ValidationError:
        return _fail(request, LAUNCH_FAILED, "Please choose your healthcare organization again.")
    data_source = DataSource.objects.filter(id=form.data_source_id).first()
    if data_source is None or not ClientDataSource.objects.filter(client=_client(), data_source=data_source).exists():
        return _fail(request, LAUNCH_FAILED, "This study connection is not available.")
    if not consenting_study_ids(patient, data_source):
        return _fail(request, LAUNCH_FAILED, "Please agree to share your records with your study first.")
    try:
        selection = module.directory.selection(form.location_id)
    except KeyError:
        return _fail(request, LAUNCH_FAILED, "That organization is not in the directory.")
    offered = JheOrmDirectory.offered_scopes(selection)
    if JheOrmDirectory.type_choice(selection.endpoint.vendor) is TypeChoice.EHR:
        chosen = offered  # the vendor's screen asks; our checklist was skipped
    else:
        chosen = tuple(s for s in offered if s in form.scopes)
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
        return _fail(request, LAUNCH_FAILED, str(e))
    return redirect(url)


def _finish(request: HttpRequest, module: Module, patient, attempt, token) -> Job:
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
        return _fail(request, *NOT_AVAILABLE)
    try:
        attempt = begin_callback(request.GET.get("state", ""), module.attempts)
    except FlowError as e:
        return _fail(request, CONNECT_FAILED, str(e))
    patient = _patient(request)
    if patient is None or str(patient.id) != attempt.participant.ref:
        logger.warning("uscdi-patient-access callback refused: the session is not the launching patient's")
        return _fail(
            request,
            CONNECT_FAILED,
            "Please open your invitation link again on this device and connect from there.",
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
        job = _finish(request, module, patient, attempt, token)
    except (FlowError, IdentifierConflict) as e:
        return _fail(request, CONNECT_FAILED, str(e))
    query = {"route": "status", "job": job.id, "source": str(job.participant.attrs.data_source_id)}
    return redirect(f"{_page()}?{urlencode(query)}")


def job_json(job: Job) -> dict:
    docs = job.documents
    return {
        "id": job.id,
        "status": job.status.value,
        "terminal": job.status.terminal,
        "data_source_id": job.participant.attrs.data_source_id,
        "source": {"brand": job.source.brand_name, "location": job.source.facility_name},
        "types": [
            {"label": o.label, "resource_type": o.resource_type, "status": o.status.value, "count": o.count}
            for o in job.outcomes
        ],
        "documents": {"status": docs.status.value, "stored": len(docs.stored), "skipped": sum(docs.skipped.values())},
        "delivered": job.delivery.receipt is not None,
        "failure": job.failure if job.status.terminal else None,
    }


@api_view(["GET"])
@authentication_classes([PatientSessionAuthentication])
@permission_classes([IsAuthenticated])
def jobs(request: Request) -> Response:
    """The patient's own pulls, newest first; `?source=` narrows to one data source."""
    patient = _patient(request)
    try:
        module = get_module()
    except NotConfigured:
        return Response({"results": []})
    source = request.query_params.get("source")
    narrowed = {"data_source_id": int(source)} if source and source.isdigit() else None
    found = module.jobs.for_participant(str(patient.id), narrowed) if patient is not None else []
    return Response({"results": [job_json(j) for j in found]})


@api_view(["GET"])
@authentication_classes([PatientSessionAuthentication])
@permission_classes([IsAuthenticated])
def job(request: Request, job_id: str) -> Response:
    patient = _patient(request)
    try:
        found = get_module().jobs.get(job_id) if patient is not None else None
    except (NotConfigured, KeyError):
        found = None
    if found is None or found.participant.ref != str(patient.id):
        return _error("Not found", 404)
    return Response(job_json(found))
