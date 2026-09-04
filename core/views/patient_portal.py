import re
from urllib.parse import quote, unquote

from django.db import transaction
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import (
    ClientDataSource,
    DataSource,
    FhirAuxResource,
    FhirSource,
    PatientInvitation,
    Study,
    StudyPatient,
    StudyPatientScopeConsent,
)
from core.services.jhe_settings import get_setting
from core.services.ow_ingest import revoke_connection_if_fully_unconsented
from core.views.ehr_patient_portal import EHR_PATIENT_PORTAL_CLIENT_NAME

SESSION_CODE_KEY = "patient_portal_code"
SESSION_INVITATION_KEY = "patient_portal_invitation_id"
SESSION_LAST_DS_KEY = "patient_portal_last_ds_id"

# DataSource.type -> bootstrap-icons glyph on its card.
_TYPE_ICONS = {
    "patient_app": "bi-file-earmark-text",
    "medical_device": "bi-activity",
    "personal_device": "bi-smartwatch",
}

# FhirAuxResource.resource_type -> receipt row label; anything else is pluralized CamelCase words.
_RESOURCE_LABEL_OVERRIDES = {
    "Patient": "Demographics",
    "MedicationRequest": "Medications",
    "MedicationDispense": "Medication dispenses",
    "AllergyIntolerance": "Allergies",
    "DiagnosticReport": "Diagnostic reports",
    "DocumentReference": "Documents",
    "ServiceRequest": "Service requests",
    "CarePlan": "Care plans",
    "CareTeam": "Care teams",
    "QuestionnaireResponse": "Questionnaire responses",
}


def _patient_label(text):
    """Scope text with a trailing "(OMH)"/"(IEEE)"-style parenthetical stripped."""
    if not text:
        return ""
    return re.sub(r"\s*\([^)]*\)\s*$", "", text)


def _invitation_from_code(code):
    """Resolve a wire-format code to a valid PatientInvitation by token hash (never redeems), or None."""
    parts = unquote(code or "").split("_", 1)
    if len(parts) != 2:
        return None
    inv = PatientInvitation.find_by_token(parts[1])
    return inv if inv is not None and inv.is_valid() else None


def _resolve_patient(request):
    """Patient from ?code= (and remember it), else from the session. Returns (patient, invitation|None, code)."""
    code = request.GET.get("code") or request.POST.get("code") or ""
    if code:
        inv = _invitation_from_code(code)
        if inv is None:
            return None, None, code
        request.session.cycle_key()  # rotate the session id so a pre-auth session can't be fixated post-auth
        request.session[SESSION_CODE_KEY] = code
        request.session[SESSION_INVITATION_KEY] = inv.pk
        window_hours = get_setting("auth.patient.invitation_redemption_window_hours", 12)
        request.session.set_expiry(window_hours * 3600)  # session can't outlive the invitation's redemption window
        return inv.patient, inv, code

    inv_id = request.session.get(SESSION_INVITATION_KEY)
    inv = PatientInvitation.objects.filter(pk=inv_id).first() if inv_id else None
    if inv is None or not inv.is_valid():  # re-check every codeless visit so revocation takes effect
        for key in (SESSION_CODE_KEY, SESSION_INVITATION_KEY):
            request.session.pop(key, None)
        return None, None, ""
    return inv.patient, inv, request.session.get(SESSION_CODE_KEY, "")


def _patient_facing_data_source_ids():
    """Ids of DataSources linked to a client whose aux_data has patient_facing=True."""
    return set(
        ClientDataSource.objects.filter(client__jhe_client__aux_data__patient_facing=True).values_list(
            "data_source_id", flat=True
        )
    )


def _patient_facing_source(data_source_id):
    """The DataSource (with supported_scopes) if a patient-facing client links it, else None."""
    if data_source_id not in _patient_facing_data_source_ids():
        return None
    ds_list = DataSource.data_sources_with_scopes(data_source_id=data_source_id)
    return ds_list[0] if ds_list else None


def _client_link(data_source_id):
    """The ClientDataSource link for a data source (lowest id if several), or None."""
    return (
        ClientDataSource.objects.filter(data_source_id=data_source_id)
        .select_related("client__jhe_client")
        .order_by("id")
        .first()
    )


def _resource_label(resource_type):
    if resource_type in _RESOURCE_LABEL_OVERRIDES:
        return _RESOURCE_LABEL_OVERRIDES[resource_type]
    return " ".join(re.findall(r"[A-Z][a-z0-9]*", resource_type) or [resource_type]) + "s"


def _expected_scope_types(data_source_id):
    """Resource types from the linked client's patient/<Type>.read scopes; empty if it has none."""
    link = _client_link(data_source_id)
    scopes = link.client.jhe_client.aux_data.get("scopes") if link else None
    if not scopes:
        return set()
    return {
        s.removeprefix("patient/").removesuffix(".read")
        for s in scopes.split()
        if s.startswith("patient/") and s.endswith(".read")
    }


def _scope_detail(data_source_id):
    """Comma-joined expected-type labels for the consent subtext, "Demographics" first; "" if none."""
    labels = sorted(_resource_label(rt) for rt in _expected_scope_types(data_source_id))
    if not labels:
        return ""
    if "Demographics" in labels:
        labels.remove("Demographics")
        labels.insert(0, "Demographics")
    return ", ".join([labels[0]] + [label.lower() for label in labels[1:]])


def _receipt(fhir_source):
    """Per-type synced counts, a zero row per promised-but-missing type, and the total."""
    counts = dict(
        FhirAuxResource.objects.filter(fhir_source=fhir_source)
        .values("resource_type")
        .annotate(n=Count("id"))
        .values_list("resource_type", "n")
    )
    synced = sorted(((_resource_label(rt), n) for rt, n in counts.items()), key=lambda row: (-row[1], row[0]))
    not_synced = sorted(_resource_label(rt) for rt in _expected_scope_types(fhir_source.data_source_id) - counts.keys())
    return {"synced": synced, "not_synced": [(label, 0) for label in not_synced], "total": sum(counts.values())}


def _card_desc(source):
    """ "facility · labels · N records" once a FhirSource with a facility exists, else the scope labels."""
    labels = ", ".join(source["labels"])
    if not source["detail"]:
        return labels
    facility, _sep, tail = source["detail"].partition(" · ")
    return f"{facility} · {labels} · {tail}"


def _sources(patient):
    """One entry per patient-facing DataSource across the patient's studies, with its consent state."""
    out = {}
    patient_facing_ids = _patient_facing_data_source_ids()
    for pending, studies in (
        (True, Study.studies_with_scopes(patient.id, pending=True)),
        (False, Study.studies_with_scopes(patient.id, pending=False)),
    ):
        for study in studies:
            scopes = study.pending_scope_consents if pending else [c for c in study.scope_consents if c["consented"]]
            revoked = [] if pending else [c for c in study.scope_consents if c["consented"] is False]
            for ds in study.data_sources:
                if ds.id not in patient_facing_ids:
                    continue
                supported = {s.id for s in ds.supported_scopes}
                hits = [c for c in scopes if c["code"]["id"] in supported]
                revoked_hits = [c for c in revoked if c["code"]["id"] in supported]
                if not hits and not revoked_hits:
                    continue
                e = out.setdefault(
                    ds.id,
                    {
                        "id": ds.id,
                        "name": ds.name,
                        "icon": _TYPE_ICONS.get(ds.type, "bi-file-earmark-text"),
                        "pending": [],
                        "consented": [],
                        "studies": set(),
                    },
                )
                e["pending" if pending else "consented"].extend(hits)
                e["pending"].extend(revoked_hits)  # a revoked scope is re-consentable, so it reads as pending
                e["studies"].add(study.name)
    for e in out.values():
        e["connected"] = not e["pending"] and bool(e["consented"])  # badge reflects consent state only
        e["labels"] = sorted({_patient_label(c["code"]["text"]) for c in e["pending"] + e["consented"]})
        e["detail"] = None
        e["fhir_source"] = None
        # A revoked source keeps its FhirSource and records, but only a consented card advertises them.
        fs = (
            FhirSource.objects.filter(patient=patient, data_source_id=e["id"])
            .select_related("ehr_brand_location")
            .order_by("-id")
            .first()
            if e["connected"]
            else None
        )
        if fs is not None:
            facility = fs.ehr_brand_location.name if fs.ehr_brand_location else fs.label
            if facility:
                e["detail"] = f"{facility} · {fs.aux_resources.count()} records"
                e["fhir_source"] = fs
    return sorted(out.values(), key=lambda e: e["name"])


def _pending_pairs(patient, ds):
    """(study, scope) pairs this source can ask about: never asked, or revoked."""
    pending = Study.scope_consents_for_data_source(patient.id, ds, pending=True)
    revoked = [(s, c) for s, c in Study.scope_consents_for_data_source(patient.id, ds) if c["consented"] is False]
    return pending + revoked


def _consented_pairs(patient, ds):
    return [(s, c) for s, c in Study.scope_consents_for_data_source(patient.id, ds) if c["consented"]]


@transaction.atomic
def _record_consent(patient, pairs):
    now = timezone.now()
    for study, c in pairs:
        study_patient = StudyPatient.objects.get(study=study, patient=patient)
        StudyPatientScopeConsent.objects.update_or_create(
            study_patient=study_patient,
            scope_code_id=c["code"]["id"],
            defaults={"consented": True, "consented_time": now},
        )


@transaction.atomic
def _revoke_consent(patient, pairs):
    now = timezone.now()
    for study, c in pairs:
        StudyPatientScopeConsent.objects.filter(
            study_patient__study=study, study_patient__patient=patient, scope_code_id=c["code"]["id"]
        ).update(consented=False, consented_time=now)


def _client_flow_url(patient, invitation, code, ds):
    """Where consent hands off: the source's own client flow, minting that client's invitation if needed."""
    link = _client_link(ds.id)
    if link is None:
        return reverse("patient-landing")
    client = link.client
    if invitation is not None and client.id == invitation.client_id:
        if client.name == EHR_PATIENT_PORTAL_CLIENT_NAME:  # its invitation_url is this hub; go to the picker
            return reverse("ehr-patient-portal-connect") + f"?code={quote(code, safe='')}"
        return client.jhe_client.invitation_url.replace("CODE", quote(code, safe=""))
    _new_invitation, url = PatientInvitation.build_link(patient, client)
    return url


def _render_invalid(request):
    return render(request, "patient/invalid_invitation.html", status=400)


def landing(request):
    patient, _invitation, _code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)

    sources = _sources(patient)
    study_names = {name for source in sources for name in source["studies"]}
    eyebrow = next(iter(study_names)) if len(study_names) == 1 else "Your studies"
    cards = [
        {
            "title": source["name"],
            "desc": _card_desc(source),
            "icon": source["icon"],
            "on": source["connected"],
            "badge": "Consented" if source["connected"] else "Not consented",
            "url": reverse("patient-manage" if source["connected"] else "patient-consent", args=[source["id"]]),
        }
        for source in sources
    ]
    return render(request, "patient/landing.html", {"eyebrow": eyebrow, "cards": cards})


def consent(request, data_source_id):
    """List this source's pending scopes; POST records consent and redirects into the source's client."""
    patient, invitation, code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)
    ds = _patient_facing_source(data_source_id)  # a crafted link must not consent to a source the hub never offers
    if ds is None:
        return _render_invalid(request)
    pairs = _pending_pairs(patient, ds)
    if not pairs:
        return redirect(reverse("patient-landing"))

    if request.method == "POST":
        _record_consent(patient, pairs)
        request.session[SESSION_LAST_DS_KEY] = ds.id
        return redirect(_client_flow_url(patient, invitation, code, ds))

    studies = sorted({study.name for study, _c in pairs})
    context = {
        "ds": ds,
        "eyebrow": f"{ds.name} · {' · '.join(studies)}",
        "rows": [_patient_label(c["code"]["text"]) for _study, c in pairs],
        "scope_detail": _scope_detail(ds.id),
    }
    return render(request, "patient/consent.html", context)


def manage(request, data_source_id):
    """List this source's consented scopes; POST revokes them all and returns to the hub."""
    patient, _invitation, _code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)
    ds = _patient_facing_source(data_source_id)
    if ds is None:
        return _render_invalid(request)
    entry = next((s for s in _sources(patient) if s["id"] == ds.id), None)
    if entry is None or not entry["consented"]:
        return redirect(reverse("patient-landing"))

    if request.method == "POST":
        pairs = _consented_pairs(patient, ds)
        _revoke_consent(patient, pairs)
        revoke_connection_if_fully_unconsented(patient, {study.id for study, _c in pairs})
        return redirect(reverse("patient-landing"))

    context = {
        "ds": ds,
        "rows": sorted({_patient_label(c["code"]["text"]) for c in entry["consented"]}),
        "icon": entry["icon"],
        "detail": _card_desc(entry) if entry["detail"] else None,
        "receipt": _receipt(entry["fhir_source"]) if entry["fhir_source"] else None,
    }
    return render(request, "patient/manage.html", context)


def done(request):
    """Show the source consent() just recorded, else the most recently consented connected source."""
    patient, _invitation, _code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)

    connected = [s for s in _sources(patient) if s["connected"]]
    last_ds_id = request.session.get(SESSION_LAST_DS_KEY)
    primary = next((s for s in connected if s["id"] == last_ds_id), None)
    if primary is None and connected:
        primary = max(connected, key=lambda s: max(c["consented_time"] for c in s["consented"]))

    rows = [{"name": primary["name"], "detail": _card_desc(primary)}] if primary is not None else []
    study = (
        next(iter(primary["studies"])) if primary is not None and len(primary["studies"]) == 1 else "your study team"
    )
    lede = (
        f"Your selected data is now shared with {study}. You can manage or disconnect any source anytime."
        if primary is not None
        else "Nothing is shared yet."
    )
    receipt = _receipt(primary["fhir_source"]) if primary is not None and primary["fhir_source"] else None
    return render(request, "patient/done.html", {"rows": rows, "lede": lede, "receipt": receipt})
