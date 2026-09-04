import re
from urllib.parse import quote, unquote

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
from core.views.ehr_patient_portal import EHR_PATIENT_PORTAL_CLIENT_NAME

SESSION_CODE_KEY = "patient_portal_code"
SESSION_INVITATION_KEY = "patient_portal_invitation_id"
SESSION_LAST_DS_KEY = "patient_portal_last_ds_id"

# DataSource.type -> the bootstrap-icons glyph its icon card renders (§E).
_TYPE_ICONS = {
    "patient_app": "bi-file-earmark-text",
    "medical_device": "bi-activity",
    "personal_device": "bi-smartwatch",
}


def _patient_label(text):
    """A scope's patient-facing label: its seeded CodeableConcept.text with one trailing
    coding-standard parenthetical (e.g. " (OMH)", " (IEEE)") stripped -- developer-facing
    plumbing patients don't need to see. Text without a trailing parenthetical is unchanged."""
    if not text:
        return text or ""
    return re.sub(r"\s*\([^)]*\)\s*$", "", text)


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
        request.session.cycle_key()  # rotate the session id so a pre-auth session can't be fixated post-auth
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
        for key in (SESSION_CODE_KEY, SESSION_INVITATION_KEY):
            request.session.pop(key, None)
        return None, None, ""
    return inv.patient, inv, request.session.get(SESSION_CODE_KEY, "")


def _patient_facing_data_source_ids():
    """DataSource ids reachable through a client whose aux_data flags patient_facing=True.

    A DataSource with no such client (e.g. CareX, Questionnaire -- direct-to-API integrations
    with no patient-facing flow of their own) must never appear on the hub. One query, so the
    per-source loop in _sources() can filter by membership instead of hitting the DB per row.
    """
    return set(
        ClientDataSource.objects.filter(client__jhe_client__aux_data__patient_facing=True).values_list(
            "data_source_id", flat=True
        )
    )


def _icon_for(ds):
    """The bootstrap-icons glyph an icon card renders for this DataSource's type (§E)."""
    return _TYPE_ICONS.get(ds.type, "bi-file-earmark-text")


# FhirAuxResource.resource_type -> the receipt row label it renders as (§D). Anything not
# listed here falls back to its CamelCase words pluralized (see _resource_label).
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


def _resource_label(resource_type):
    """Human label for a receipt row (§D): an override, or the type's CamelCase words
    pluralized (Observation -> Observations, Device -> Devices)."""
    if resource_type in _RESOURCE_LABEL_OVERRIDES:
        return _RESOURCE_LABEL_OVERRIDES[resource_type]
    return " ".join(re.findall(r"[A-Z][a-z0-9]*", resource_type) or [resource_type]) + "s"


def _expected_scope_types(data_source_id):
    """Resource types the linked client's SMART scopes promise to sync (§I) -- parsed from
    "patient/<Type>.read" entries in its aux_data["scopes"]; empty when the client carries no
    scopes, in which case the receipt shows only what's actually landed, with no "Not synced"
    section."""
    link = (
        ClientDataSource.objects.filter(data_source_id=data_source_id)
        .select_related("client__jhe_client")
        .order_by("id")
        .first()
    )
    scopes = link.client.jhe_client.aux_data.get("scopes") if link else None
    if not scopes:
        return set()
    return {
        s.removeprefix("patient/").removesuffix(".read")
        for s in scopes.split()
        if s.startswith("patient/") and s.endswith(".read")
    }


def _scope_detail(data_source_id):
    """Consent-screen subtext (pe-*): the humanized expected resource types for this data
    source's linked client, as a lowercase sentence fragment ("Demographics, conditions,
    medications, ...") with "Demographics" kept first when present; "" when the client carries
    no scopes, so the template renders no subtext at all."""
    labels = sorted(_resource_label(rt) for rt in _expected_scope_types(data_source_id))
    if not labels:
        return ""
    if "Demographics" in labels:
        labels.remove("Demographics")
        labels.insert(0, "Demographics")
    return ", ".join([labels[0]] + [label.lower() for label in labels[1:]])


def _receipt(fhir_source):
    """The per-type sync receipt for a connected FhirSource (pa-05): synced counts (highest
    first, then label) plus a zero row for any type the client's scopes promised that hasn't
    landed yet, and the total count of resources actually synced."""
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
    """The hub/done description for a source: "facility · labels · N records" once a FhirSource
    with a facility exists, else just the comma-joined scope labels."""
    labels = ", ".join(source["labels"])
    if not source["detail"]:
        return labels
    facility, _sep, tail = source["detail"].partition(" · ")
    return f"{facility} · {labels} · {tail}"


def _sources(patient):
    """One entry per patient-facing DataSource across the patient's studies: pending +
    consented scope labels, studies, and (when connected through a registered FhirSource) the
    facility and record count the patient imported from.

    A revoked scope (a consent row with consented=False) is folded into "pending" rather than
    dropped -- it needs to be re-consentable, so the source must read as Not connected and its
    card must link back to consent, exactly like a scope that was never asked about.
    """
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
                e = out.setdefault(ds.id, {"id": ds.id, "name": ds.name, "pending": [], "consented": [], "studies": set()})
                e["pending" if pending else "consented"].extend(hits)
                e["pending"].extend(revoked_hits)
                e["studies"].add(study.name)
    for e in out.values():
        e["connected"] = not e["pending"] and bool(e["consented"])  # badge = consent state (demo definition)
        e["labels"] = sorted({_patient_label(c["code"]["text"]) for c in e["pending"] + e["consented"]})
        e["detail"] = None
        e["fhir_source"] = None
        # A revoked source keeps its FhirSource and imported records, but its card must not
        # keep advertising the facility and record count: Not consented reads as scopes only.
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
                count = fs.aux_resources.count()
                e["detail"] = f"{facility} · {count} records"
                e["fhir_source"] = fs
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
                "desc": _card_desc(source),
                "badge": "Consented" if source["connected"] else "Not consented",
                "url": url,
            }
        )

    return render(request, "patient/landing.html", {"eyebrow": eyebrow, "cards": cards})


def _pending_pairs(patient, ds):
    """(study, scope_consent) for each of the patient's not-currently-consented scopes this data
    source supports -- scopes never asked about, plus any that were consented and then revoked
    (consented=False), so a revoked source is still re-consentable via this same screen."""
    supported = {s.id for s in ds.supported_scopes}
    pairs = [
        (study, c)
        for study in Study.studies_with_scopes(patient.id, pending=True)
        if _study_uses(study, ds)
        for c in study.pending_scope_consents
        if c["code"]["id"] in supported
    ]
    pairs += [
        (study, c)
        for study in Study.studies_with_scopes(patient.id, pending=False)
        if _study_uses(study, ds)
        for c in study.scope_consents
        if c["code"]["id"] in supported and c["consented"] is False
    ]
    return pairs


def _study_uses(study, ds):
    """Whether the study collects through this data source (StudyDataSource link) -- the same
    rule _sources() applies, so a scope another study requests from a different source (e.g.
    Heart Rate via CareX) never rides along on this source's consent or revoke."""
    return any(d.id == ds.id for d in study.data_sources)


def _consented_pairs(patient, ds):
    """(study, scope_consent) for each of the patient's actively-consented scopes this data
    source supports -- the rows a "Stop sharing" POST needs to revoke."""
    supported = {s.id for s in ds.supported_scopes}
    return [
        (study, c)
        for study in Study.studies_with_scopes(patient.id, pending=False)
        if _study_uses(study, ds)
        for c in study.scope_consents
        if c["code"]["id"] in supported and c["consented"]
    ]


def consent(request, data_source_id):
    """"What you'll share" (pe-3): list this source's pending scopes; on POST record consent
    for each, then route into the source's own client (same client as the invitation reuses
    its code; a different client gets its own invitation minted server-side)."""
    patient, invitation, code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)

    # Same patient_facing gate _sources()/manage() enforce -- without it a crafted link could
    # consent to a source the hub never offers (e.g. CareX, a direct-to-API integration).
    if data_source_id not in _patient_facing_data_source_ids():
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

        request.session[SESSION_LAST_DS_KEY] = ds.id  # done() leads with this source (§G)

        link = (
            ClientDataSource.objects.filter(data_source=ds)
            .select_related("client__jhe_client")
            .order_by("id")  # deterministic pick if a DataSource is ever linked to more than one client
            .first()
        )
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
                url = client.jhe_client.invitation_url.replace("CODE", quote(code, safe=""))
        else:
            _new_invitation, url = PatientInvitation.build_link(patient, client)

        return redirect(url)

    studies = sorted({study.name for study, _c in pairs})
    context = {
        "ds": ds,
        "eyebrow": f"{ds.name} · {' · '.join(studies)}",
        "rows": [_patient_label(c["code"]["text"]) for _study, c in pairs],
        "code": code,
        "scope_detail": _scope_detail(ds.id),
    }
    return render(request, "patient/consent.html", context)


def manage(request, data_source_id):
    """"You're sharing" (manage/revoke): list this source's consented scopes; on POST revoke
    them all (consented=False) and send the patient back to the hub, where the source reads as
    Not connected again and is re-consentable. No vendor-side disconnect is attempted here --
    that's the API's best-effort hook, out of scope for this server-rendered flow."""
    patient, _invitation, code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)

    ds_list = DataSource.data_sources_with_scopes(data_source_id=data_source_id)
    if not ds_list:
        return _render_invalid(request)
    ds = ds_list[0]

    entry = next((s for s in _sources(patient) if s["id"] == ds.id), None)
    if entry is None or not entry["consented"]:
        return _render_invalid(request)

    if request.method == "POST":
        now = timezone.now()
        for study, c in _consented_pairs(patient, ds):
            StudyPatientScopeConsent.objects.filter(
                study_patient__study=study,
                study_patient__patient=patient,
                scope_code_id=c["code"]["id"],
            ).update(consented=False, consented_time=now)
        return redirect(reverse("patient-landing"))

    rows = sorted({_patient_label(c["code"]["text"]) for c in entry["consented"]})
    context = {
        "ds": ds,
        "rows": rows,
        "code": code,
        "icon_class": _icon_for(ds),
        # A registered FhirSource's facility/record-count line (§D), shown once above the
        # per-scope rows; None when there's nothing more specific than the scopes already say.
        "detail": _card_desc(entry) if entry["detail"] else None,
        "receipt": _receipt(entry["fhir_source"]) if entry["fhir_source"] else None,
    }
    return render(request, "patient/manage.html", context)


def done(request):
    """"You're all set" (pe-7): leads with -- and shows only -- the source the patient just
    connected (the one consent() recorded into the session); with no such marker (e.g. this
    session only ever hit the hub) it falls back to whichever connected source was consented
    most recently, never "every connected source" (§G)."""
    patient, _invitation, _code = _resolve_patient(request)
    if patient is None:
        return _render_invalid(request)

    connected = [s for s in _sources(patient) if s["connected"]]
    last_ds_id = request.session.get(SESSION_LAST_DS_KEY)
    primary = next((s for s in connected if s["id"] == last_ds_id), None)
    if primary is None and connected:
        primary = max(connected, key=lambda s: max(c["consented_time"] for c in s["consented"]))

    rows = [{"name": primary["name"], "detail": _card_desc(primary)}] if primary is not None else []

    study = next(iter(primary["studies"])) if primary is not None and len(primary["studies"]) == 1 else "your study team"
    lede = (
        f"Your selected data is now shared with {study}. You can manage or disconnect any source anytime."
        if primary is not None
        else "Nothing is shared yet."
    )
    receipt = _receipt(primary["fhir_source"]) if primary is not None and primary["fhir_source"] else None

    return render(request, "patient/done.html", {"rows": rows, "lede": lede, "receipt": receipt})
