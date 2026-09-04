from urllib.parse import quote, unquote

from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import (
    ClientDataSource,
    DataSource,
    PatientInvitation,
    Study,
    StudyPatient,
    StudyPatientScopeConsent,
)
from core.services.jhe_settings import get_setting
from core.views.ehr_patient_portal import EHR_PATIENT_PORTAL_CLIENT_NAME

SESSION_KEY = "patient_portal_patient_id"
SESSION_CODE_KEY = "patient_portal_code"
SESSION_INVITATION_KEY = "patient_portal_invitation_id"


def _invitation_is_valid(inv):
    """Mirrors PatientInvitation.redeem()'s validity window, without mutating status."""
    if inv.status == PatientInvitation.Status.ISSUED:
        expiration_days = get_setting("auth.patient.invitation_expiration_days", 7)
        return (timezone.now() - inv.last_updated).days < expiration_days
    if inv.status == PatientInvitation.Status.REDEEMED:
        window_hours = get_setting("auth.patient.invitation_redemption_window_hours", 12)
        return (timezone.now() - inv.last_updated).total_seconds() / 3600 <= window_hours
    return False  # CANCELLED, EXPIRED, REISSUED


def _invitation_from_code(code):
    """The PatientInvitation the wire-format code names, or None if it can't be resolved.

    Looks up by token hash only -- it never redeems, so the client flow's own redeem step
    (which consumes the invitation) still works after a landing/consent page visit.
    """
    parts = unquote(code or "").split("_", 1)
    if len(parts) != 2:
        return None
    inv = (
        PatientInvitation.objects.select_related("patient__jhe_user", "client__jhe_client")
        .filter(token_hash=PatientInvitation._hash_token(parts[1]))
        .first()
    )
    return inv if inv is not None and _invitation_is_valid(inv) else None


def _resolve_patient(request):
    """Patient from ?code= (and remember it), else from the session. Returns (patient, invitation|None, code)."""
    code = request.GET.get("code") or request.POST.get("code") or ""
    if code:
        inv = _invitation_from_code(code)
        if inv is None:
            return None, None, code
        request.session[SESSION_KEY] = inv.patient_id
        request.session[SESSION_CODE_KEY] = code
        request.session[SESSION_INVITATION_KEY] = inv.pk
        window_hours = get_setting("auth.patient.invitation_redemption_window_hours", 12)
        request.session.set_expiry(window_hours * 3600)  # session can't outlive the invitation's redemption window
        return inv.patient, inv, code

    inv_id = request.session.get(SESSION_INVITATION_KEY)
    inv = (
        PatientInvitation.objects.select_related("patient", "client__jhe_client").filter(pk=inv_id).first()
        if inv_id
        else None
    )
    if inv is None or not _invitation_is_valid(inv):  # re-check every codeless visit so revocation takes effect
        for key in (SESSION_KEY, SESSION_CODE_KEY, SESSION_INVITATION_KEY):
            request.session.pop(key, None)
        return None, None, ""
    return inv.patient, inv, request.session.get(SESSION_CODE_KEY, "")


def _sources(patient):
    """One entry per DataSource across the patient's studies: pending + consented scope labels, studies."""
    out = {}
    for pending, studies in (
        (True, Study.studies_with_scopes(patient.id, pending=True)),
        (False, Study.studies_with_scopes(patient.id, pending=False)),
    ):
        for study in studies:
            scopes = study.pending_scope_consents if pending else [c for c in study.scope_consents if c["consented"]]
            for ds in study.data_sources:
                supported = {s.id for s in ds.supported_scopes}
                hits = [c for c in scopes if c["code"]["id"] in supported]
                if not hits:
                    continue
                e = out.setdefault(ds.id, {"id": ds.id, "name": ds.name, "pending": [], "consented": [], "studies": set()})
                e["pending" if pending else "consented"].extend(hits)
                e["studies"].add(study.name)
    for e in out.values():
        e["connected"] = not e["pending"] and bool(e["consented"])  # badge = consent state (demo definition)
        e["labels"] = sorted({c["code"]["text"] for c in e["pending"] + e["consented"]})
    return list(out.values())


def _render_invalid(request):
    return render(request, "patient/invalid_invitation.html", status=400)


def landing(request):
    """The "Choose how to share your data" hub (pe-2): one card per source, badged by consent state."""
    patient, _invitation, code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)

    sources = _sources(patient)
    study_names = {name for source in sources for name in source["studies"]}
    eyebrow = next(iter(study_names)) if len(study_names) == 1 else "Your studies"

    cards = []
    for source in sources:
        route = "patient-manage" if source["connected"] else "patient-consent"
        url = reverse(route, args=[source["id"]])
        if code:
            url = f"{url}?code={quote(code, safe='')}"
        cards.append(
            {
                "title": source["name"],
                "desc": ", ".join(source["labels"]),
                "badge": "Connected" if source["connected"] else "Not connected",
                "url": url,
            }
        )

    return render(request, "patient/landing.html", {"eyebrow": eyebrow, "cards": cards})


def _pending_pairs(patient, ds):
    """(study, scope_consent) for each of the patient's pending scopes this data source supports."""
    supported = {s.id for s in ds.supported_scopes}
    return [
        (study, c)
        for study in Study.studies_with_scopes(patient.id, pending=True)
        for c in study.pending_scope_consents
        if c["code"]["id"] in supported
    ]


def consent(request, data_source_id):
    """"What you'll share" (pe-3): list this source's pending scopes; on POST record consent
    for each, then route into the source's own client (same client as the invitation reuses
    its code; a different client gets its own invitation minted server-side)."""
    patient, invitation, code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)

    ds_list = DataSource.data_sources_with_scopes(data_source_id=data_source_id)
    if not ds_list:
        return _render_invalid(request)
    ds = ds_list[0]
    pairs = _pending_pairs(patient, ds)
    if not pairs:
        return _render_invalid(request)

    if request.method == "POST":
        now = timezone.now()
        for study, c in pairs:
            study_patient = StudyPatient.objects.get(study=study, patient=patient)
            obj, created = StudyPatientScopeConsent.objects.get_or_create(
                study_patient=study_patient,
                scope_code_id=c["code"]["id"],
                defaults={"consented": True, "consented_time": now},
            )
            if not created and not obj.consented:
                obj.consented = True
                obj.consented_time = now
                obj.save()

        link = ClientDataSource.objects.filter(data_source=ds).select_related("client__jhe_client").first()
        if link is None:
            return redirect(reverse("patient-landing"))
        client = link.client

        if invitation is not None and client.id == invitation.client_id:
            # Same client as the invitation: reuse its own invitation_url -- except the EHR
            # Patient Portal's invitation_url points back at this hub (/patient/), which would
            # loop the patient back here instead of the hospital picker, so send it there.
            if client.name == EHR_PATIENT_PORTAL_CLIENT_NAME:
                url = reverse("ehr-patient-portal-connect") + f"?code={quote(code, safe='')}"
            else:
                url = client.jhe_client.invitation_url.replace("CODE", code)
        else:
            _new_invitation, url = PatientInvitation.build_link(patient, client)

        return redirect(url)

    studies = sorted({study.name for study, _c in pairs})
    context = {
        "ds": ds,
        "eyebrow": f"{ds.name} · {' · '.join(studies)}",
        "rows": [c for _study, c in pairs],
        "code": code,
    }
    return render(request, "patient/consent.html", context)


def manage(request, data_source_id):
    """Stub: manage/revoke screen lands in Task 15."""
    patient, _invitation, _code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)
    return _render_invalid(request)


def done(request):
    """Stub: done screen lands in Task 15."""
    patient, _invitation, _code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)
    return _render_invalid(request)
