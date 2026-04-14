import logging

from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from oauth2_provider.models import Grant

import core.ow_client as ow_client
from core.jhe_settings.service import get_setting
from core.models import Patient, PatientWearableConnection, StudyClient, StudyPatient

logger = logging.getLogger(__name__)

AVAILABLE_SCOPES = [{"code": "heart_rate", "label": "Heart Rate"}]


def consent(request):
    code = request.GET.get("code") or request.POST.get("code")
    patient, study = _resolve_invitation_code(code)

    if patient is None or study is None:
        return render(request, "client/ow_consent.html", {"error": "Invalid or expired invite link."})

    study_patient = StudyPatient.objects.filter(study=study, patient=patient).first()
    if not study_patient:
        return render(
            request, "client/ow_consent.html", {"error": "You are not enrolled in this study.", "study": study}
        )

    connection = PatientWearableConnection.objects.filter(patient=patient, provider="oura").first()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "revoke":
            return _handle_revoke(request, patient, connection, study, code)
        return _handle_consent(request, patient, study, code)

    context = {
        "study": study,
        "patient": patient,
        "connection": connection,
        "available_scopes": AVAILABLE_SCOPES,
        "consented_scopes": connection.consented_scopes if connection else [],
        "code": code,
    }
    return render(request, "client/ow_consent.html", context)


def consent_success(request):
    revoked = request.GET.get("action") == "revoked"
    return render(request, "client/ow_success.html", {"revoked": revoked})


def oauth_callback(request):
    ow_user_id = request.GET.get("ow_user_id")
    if not ow_user_id:
        return render(request, "client/ow_consent.html", {"error": "Missing callback parameters."})
    if not PatientWearableConnection.objects.filter(ow_user_id=ow_user_id).exists():
        logger.warning(f"OAuth callback received for unknown ow_user_id: {ow_user_id}")
        return render(request, "client/ow_consent.html", {"error": "Unknown OAuth callback."})

    site_url = get_setting("site.url", "http://localhost:8000")
    return HttpResponseRedirect(f"{site_url}/ow/success")


def _handle_consent(request, patient, study, code):
    submitted = set(request.POST.getlist("scopes"))
    valid_codes = {s["code"] for s in AVAILABLE_SCOPES}
    selected_scopes = sorted(submitted & valid_codes)

    if not selected_scopes:
        return render(
            request,
            "client/ow_consent.html",
            {
                "study": study,
                "patient": patient,
                "available_scopes": AVAILABLE_SCOPES,
                "code": code,
                "error": "Please select at least one data scope.",
            },
        )

    ow_user_id = ow_client.create_user(patient.jhe_user.email)
    site_url = get_setting("site.url", "http://localhost:8000")
    redirect_uri = f"{site_url}/ow/callback?ow_user_id={ow_user_id}&code={code}"

    PatientWearableConnection.objects.update_or_create(
        patient=patient,
        provider="oura",
        defaults={"ow_user_id": ow_user_id, "consented_scopes": selected_scopes},
    )

    authorize_url = ow_client.get_authorize_url("oura", ow_user_id, redirect_uri)
    return HttpResponseRedirect(authorize_url)


def _handle_revoke(request, patient, connection, study, code):
    if connection:
        try:
            ow_client.revoke_connection(connection.ow_user_id, connection.provider)
        except Exception:
            logger.exception(f"Failed to revoke OW connection for patient {patient.id}")
            return render(
                request,
                "client/ow_consent.html",
                {
                    "study": study,
                    "patient": patient,
                    "connection": connection,
                    "code": code,
                    "error": "Failed to revoke with Open Wearables. Please try again.",
                },
            )
        with transaction.atomic():
            connection.delete()
    site_url = get_setting("site.url", "http://localhost:8000")
    return HttpResponseRedirect(f"{site_url}/ow/success?action=revoked")


def _resolve_invitation_code(code):
    if not code:
        return None, None

    parts = code.split("~")
    if len(parts) != 4:
        return None, None
    _host, client_id, auth_code, _code_verifier = parts

    try:
        grant = Grant.objects.filter(code=auth_code).select_related("user").first()
        if not grant:
            return None, None
        patient = Patient.objects.filter(jhe_user=grant.user).first()
        if not patient:
            return None, None
        study_client = StudyClient.objects.filter(client__client_id=client_id).first()
        if not study_client:
            return None, None
        return patient, study_client.study
    except Exception:
        logger.exception("Database error resolving invitation code")
        return None, None
