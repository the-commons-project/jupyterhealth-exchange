import io
from datetime import timedelta
from urllib.parse import quote

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from oauth2_provider.models import get_application_model

from core.models import (
    CodeableConcept,
    DataSource,
    EhrBrandLocation,
    FhirAuxResource,
    FhirSource,
    JheClient,
    Patient,
    PatientInvitation,
    Study,
    StudyDataSource,
    StudyPatient,
    StudyPatientScopeConsent,
    StudyScopeRequest,
)
from core.views.patient_portal import (
    SESSION_INVITATION_KEY,
    _invitation_from_code,
    _patient_label,
    _scope_detail,
    _sources,
)

Application = get_application_model()


def _mint(patient, client):
    """Mint an invitation exactly as the API does, returning the wire-format code (the part
    of the link after `code=`, still percent-encoded -- e.g. "localhost%3A8001_<token>")."""
    _invitation, link = PatientInvitation.build_link(patient, client)
    return link.split("code=", 1)[1]


def _card_block(html, title):
    """The <a class="pf-card-link">...</a> block for the card whose title is `title`, so
    badge/href assertions can be scoped to one card instead of the whole page (other seeded
    sources may already be Connected)."""
    for block in html.split('<a class="pf-card-link"')[1:]:
        card = block.split("</a>")[0]
        if title in card:
            return card
    raise AssertionError(f"no pf-card-link block found for {title!r}")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sleep episode (IEEE)", "Sleep episode"),
        ("Heart Rate (OMH)", "Heart Rate"),
        ("Clinical records", "Clinical records"),
        ("A (b) c", "A (b) c"),
    ],
)
def test_patient_label_strips_one_trailing_parenthetical(text, expected):
    assert _patient_label(text) == expected


def test_seed_requests_clinical_records_for_lifespan_bp_hr(db):
    call_command("seed", stdout=io.StringIO())

    ds = DataSource.objects.get(name="EHR Patient Portal")
    study = Study.objects.get(name="Lifespan Study on BP & HR")
    assert StudyDataSource.objects.filter(study=study, data_source=ds).exists()

    star = CodeableConcept.objects.get(coding_system="http://hl7.org/fhir/resource-types", coding_code="*")
    assert StudyScopeRequest.objects.filter(study=study, scope_code=star).exists()
    assert star.text == "Clinical records"

    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    pending = Study.studies_with_scopes(pamela.id, pending=True)
    assert any(
        s.id == study.id and any(c["code"]["coding_code"] == "*" for c in s.pending_scope_consents) for s in pending
    )

    assert JheClient.objects.get(application__name="EHR Patient Portal").invitation_url.endswith("/patient/?code=CODE")


def test_invitation_from_code_resolves_patient_without_redeeming(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    inv = _invitation_from_code(code)

    assert inv is not None
    assert inv.patient_id == pamela.id
    assert inv.client_id == ehr_client.id
    assert inv.status == PatientInvitation.Status.ISSUED


@pytest.mark.parametrize("code", ["", "garbage", "localhost%3A8001_nope"])
def test_landing_rejects_invalid_codes(db, code):
    call_command("seed", stdout=io.StringIO())

    resp = Client().get(f"/patient/?code={code}")

    assert resp.status_code == 400
    assert "invitation" in resp.content.decode().lower()


def test_landing_lists_ehr_source_as_not_connected(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    resp = Client().get(f"/patient/?code={code}")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Choose how to share your data" in html
    assert "EHR Patient Portal" in html
    assert "Clinical records" in html
    assert "Not consented" in html
    assert "pf-card__badge" in html
    assert f"/patient/consent/{ds.id}/?code={code}" in html
    assert "pf-back" not in html  # the hub itself gets no back link (§H)

    card = _card_block(html, "EHR Patient Portal")
    assert "Not consented" in card
    assert "pf-card__badge--on" not in card


def test_session_remembers_patient_for_a_codeless_visit(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    client = Client()
    resp = client.get(f"/patient/?code={code}")

    assert resp.status_code == 200
    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_client, status=PatientInvitation.Status.ISSUED)
    assert client.session[SESSION_INVITATION_KEY] == inv.pk

    resp2 = client.get("/patient/")

    assert resp2.status_code == 200
    assert "EHR Patient Portal" in resp2.content.decode()


def test_valid_code_cycles_the_session_key(db):
    """A pre-existing (pre-auth) session must not survive redemption of a valid code --
    otherwise an attacker who plants a session id in the victim's browser before they redeem
    an invitation link could hijack the now-authenticated session (session fixation)."""
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    client = Client()
    s = client.session
    s["x"] = 1
    s.save()
    old = s.session_key

    resp = client.get(f"/patient/?code={code}")

    assert resp.status_code == 200
    assert client.session.session_key != old
    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_client, status=PatientInvitation.Status.ISSUED)
    assert client.session[SESSION_INVITATION_KEY] == inv.pk


def test_landing_shows_connected_after_consenting_clinical_records(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    study = Study.objects.get(name="Lifespan Study on BP & HR")
    star = CodeableConcept.objects.get(coding_system="http://hl7.org/fhir/resource-types", coding_code="*")
    study_patient = StudyPatient.objects.get(study=study, patient=pamela)
    StudyPatientScopeConsent.objects.create(
        study_patient=study_patient, scope_code=star, consented=True, consented_time=timezone.now()
    )

    resp = Client().get(f"/patient/?code={code}")

    assert resp.status_code == 200
    card = _card_block(resp.content.decode(), "EHR Patient Portal")
    assert "Consented" in card
    assert "pf-card__badge--on" in card
    assert f"/patient/manage/{ds.id}/?code={code}" in card


def test_landing_shows_fhir_source_facility_and_record_count(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    client.get(f"/patient/?code={code}")
    client.post(f"/patient/consent/{ds.id}/", {"code": code})

    location = EhrBrandLocation.objects.get(name="Epic Sandbox - Madison Campus")
    fhir_source = FhirSource.objects.create(patient=pamela, data_source=ds, ehr_brand_location=location)
    for _ in range(3):
        FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Observation")

    resp = client.get(f"/patient/?code={code}")

    assert resp.status_code == 200
    card = _card_block(resp.content.decode(), "EHR Patient Portal")
    assert "Epic Sandbox - Madison Campus" in card
    assert "3 records" in card


def test_peters_landing_shows_oura_and_your_studies_eyebrow(db):
    call_command("seed", stdout=io.StringIO())
    peter = Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(peter, ehr_client)

    resp = Client().get(f"/patient/?code={code}")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Your studies" in html
    card = _card_block(html, "Oura")
    # Peter is seeded already consenting the Sleep episode (IEEE) scope Oura supports, with
    # nothing pending for that source, so -- unlike the EHR Patient Portal card -- Oura shows
    # Consented here; verified against the actual seeded consent rows, not assumed.
    assert "pf-card__badge--on" in card
    assert "Consented" in card
    assert "Sleep episode" in card  # patient-facing label, coding-standard suffix stripped
    assert "(IEEE)" not in card


def test_landing_hides_non_patient_facing_sources(db):
    """CareX and Questionnaire are direct-to-API integrations with no patient-facing flow of
    their own (their client, CareX, carries no patient_facing aux_data flag), so item J keeps
    them off the hub even though Peter has scopes seeded through both (§J)."""
    call_command("seed", stdout=io.StringIO())
    peter = Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(peter, ehr_client)

    resp = Client().get(f"/patient/?code={code}")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "EHR Patient Portal" in html
    assert "Oura" in html
    assert "CareX" not in html
    assert "Questionnaire" not in html


def test_seed_flags_ehr_and_ow_clients_as_patient_facing(db):
    call_command("seed", stdout=io.StringIO())
    ehr_client = JheClient.objects.get(application__name="EHR Patient Portal")
    ow_client = JheClient.objects.get(application__name="Open Wearables")
    carex_client = JheClient.objects.get(application__name="CareX")
    assert ehr_client.aux_data.get("patient_facing") is True
    assert ow_client.aux_data.get("patient_facing") is True
    assert not (carex_client.aux_data or {}).get("patient_facing")


def test_consent_get_lists_pending_clinical_records_row(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    resp = Client().get(f"/patient/consent/{ds.id}/?code={code}")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "What you" in html and "share" in html
    assert "Clinical records" in html
    assert "Agree and share" in html
    assert 'name="code"' in html
    assert "csrfmiddlewaretoken" in html
    assert "pf-card__icon" in html and "pf-actions" in html
    assert "pf-back" in html and 'href="/patient/"' in html  # back link to the hub (§H)


def test_consent_get_shows_data_driven_scope_subtext(db):
    """The scope-row subtext is the humanized list of resource types the EHR Patient Portal's
    SMART scopes promise to sync (pe-*), not the old placeholder "Included"."""
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    resp = Client().get(f"/patient/consent/{ds.id}/?code={code}")

    html = resp.content.decode()
    assert "Demographics, " in html
    assert "observations" in html
    assert "Included" not in html


def test_consent_get_strips_coding_standard_suffix_from_scope_labels(db):
    """Oura's scope-row titles are seeded as "Heart Rate (OMH)" / "Sleep episode (IEEE)" -- the
    consent screen must show the patient-facing label only, not the developer-facing coding
    system suffix."""
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    oura_ds = DataSource.objects.get(name="Oura")
    hr_code = CodeableConcept.objects.get(coding_code="omh:heart-rate:2.0")
    sleep_code = CodeableConcept.objects.get(coding_code="ieee:sleep-episode:1.0")

    # Pamela is seeded already consenting both of Oura's scopes -- revoke them so there's
    # something pending to show on the consent screen.
    StudyPatientScopeConsent.objects.filter(
        study_patient__patient=pamela, scope_code__in=[hr_code, sleep_code]
    ).update(consented=False)

    code = _mint(pamela, ehr_client)

    resp = Client().get(f"/patient/consent/{oura_ds.id}/?code={code}")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Heart Rate" in html
    assert "Sleep episode" in html
    assert "(OMH)" not in html
    assert "(IEEE)" not in html


def test_scope_detail_is_empty_for_a_client_with_no_scopes(db):
    """Open Wearables carries no SMART "scopes" in its aux_data (Oura is polled server-side, not
    synced via patient/*.read grants), so its subtext must be empty -- the template then omits
    the subtext element entirely rather than rendering a blank row."""
    call_command("seed", stdout=io.StringIO())
    oura_ds = DataSource.objects.get(name="Oura")

    assert _scope_detail(oura_ds.id) == ""


def test_consent_get_rejects_source_with_nothing_pending(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    # CareX is fully consented for Pamela already (see seed): nothing left to ask her about.
    carex_ds = DataSource.objects.get(name="CareX")
    code = _mint(pamela, ehr_client)

    resp = Client().get(f"/patient/consent/{carex_ds.id}/?code={code}")

    assert resp.status_code == 400
    assert "invitation" in resp.content.decode().lower()


def test_consent_post_creates_consent_and_routes_to_connect_page(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    resp = Client().post(f"/patient/consent/{ds.id}/", {"code": code})

    assert resp.status_code == 302
    assert resp.url == f"/clients/ehr-patient-portal/?code={quote(code, safe='')}"

    study = Study.objects.get(name="Lifespan Study on BP & HR")
    star = CodeableConcept.objects.get(coding_system="http://hl7.org/fhir/resource-types", coding_code="*")
    study_patient = StudyPatient.objects.get(study=study, patient=pamela)
    consent_row = StudyPatientScopeConsent.objects.get(study_patient=study_patient, scope_code=star)
    assert consent_row.consented is True

    source = next(s for s in _sources(pamela) if s["id"] == ds.id)
    assert source["connected"] is True

    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_client)
    assert inv.status == PatientInvitation.Status.ISSUED


def test_consent_post_is_idempotent(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    first = Client().post(f"/patient/consent/{ds.id}/", {"code": code})
    assert first.status_code == 302

    # Nothing is pending for this source any more, so the second POST is a no-op that reports
    # "nothing to consent to" rather than re-processing -- it must not error or duplicate rows.
    second = Client().post(f"/patient/consent/{ds.id}/", {"code": code})
    assert second.status_code == 400

    study = Study.objects.get(name="Lifespan Study on BP & HR")
    star = CodeableConcept.objects.get(coding_system="http://hl7.org/fhir/resource-types", coding_code="*")
    study_patient = StudyPatient.objects.get(study=study, patient=pamela)
    assert StudyPatientScopeConsent.objects.filter(study_patient=study_patient, scope_code=star).count() == 1


def test_consent_post_cross_client_mints_open_wearables_invitation(db):
    """Cross-client routing (consenting a source whose client differs from the invitation's).
    CareX no longer qualifies -- it isn't patient-facing (§J) and consent() no longer needs to
    reach it via the hub flow -- so this now exercises Oura/Open Wearables against an
    EHR Patient Portal invitation instead."""
    call_command("seed", stdout=io.StringIO())
    peter = Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ow_client = Application.objects.get(name="Open Wearables")
    oura_ds = DataSource.objects.get(name="Oura")
    study = Study.objects.get(name="Lifespan Study on Sleep & BP")
    sleep_code = CodeableConcept.objects.get(coding_code="ieee:sleep-episode:1.0")

    # Peter is seeded already consenting Oura's sleep scope -- revoke it so there's something
    # pending to re-consent to via this (different) client's invitation code.
    study_patient = StudyPatient.objects.get(study=study, patient=peter)
    StudyPatientScopeConsent.objects.filter(study_patient=study_patient, scope_code=sleep_code).update(consented=False)

    code = _mint(peter, ehr_client)

    resp = Client().post(f"/patient/consent/{oura_ds.id}/", {"code": code})

    assert resp.status_code == 302
    assert resp.url.startswith("http://localhost:8001/clients/ow/launch?code=")

    inv = PatientInvitation.objects.get(patient=peter, client=ow_client)
    assert inv.status == PatientInvitation.Status.ISSUED

    consent_row = StudyPatientScopeConsent.objects.get(study_patient=study_patient, scope_code=sleep_code)
    assert consent_row.consented is True

    source = next(s for s in _sources(peter) if s["id"] == oura_ds.id)
    assert source["pending"] == []


def test_consent_post_same_client_non_ehr_percent_encodes_code(db):
    """The "same client as the invitation" routing branch for a non-EHR client (e.g. Open
    Wearables) must percent-encode the code into its invitation_url, exactly like the EHR
    self-link and cross-client (build_link) branches already do."""
    call_command("seed", stdout=io.StringIO())
    peter = Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")
    ow_client = Application.objects.get(name="Open Wearables")
    oura_ds = DataSource.objects.get(name="Oura")
    study = Study.objects.get(name="Lifespan Study on Sleep & BP")
    sleep_code = CodeableConcept.objects.get(coding_code="ieee:sleep-episode:1.0")

    # Peter is seeded already consenting Oura's sleep scope -- revoke it (same effect as the
    # manage() "Stop sharing" POST) so there's something pending to re-consent to.
    study_patient = StudyPatient.objects.get(study=study, patient=peter)
    StudyPatientScopeConsent.objects.filter(study_patient=study_patient, scope_code=sleep_code).update(consented=False)

    code = _mint(peter, ow_client)  # invitation minted by the *same* client the source routes back to

    resp = Client().post(f"/patient/consent/{oura_ds.id}/", {"code": code})

    assert resp.status_code == 302
    assert resp.url == f"http://localhost:8001/clients/ow/launch?code={quote(code, safe='')}"


def test_consent_post_without_code_or_session_is_invalid(db):
    call_command("seed", stdout=io.StringIO())
    ds = DataSource.objects.get(name="EHR Patient Portal")

    resp = Client().post(f"/patient/consent/{ds.id}/", {})

    assert resp.status_code == 400


def test_landing_rejects_issued_invitation_past_expiration_window(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_client, status=PatientInvitation.Status.ISSUED)
    PatientInvitation.objects.filter(pk=inv.pk).update(last_updated=timezone.now() - timedelta(days=8))

    resp = Client().get(f"/patient/?code={code}")

    assert resp.status_code == 400


def test_landing_rejects_redeemed_invitation_past_redemption_window(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_client, status=PatientInvitation.Status.ISSUED)
    PatientInvitation.objects.filter(pk=inv.pk).update(
        status=PatientInvitation.Status.REDEEMED, last_updated=timezone.now() - timedelta(hours=13)
    )

    resp = Client().get(f"/patient/?code={code}")

    assert resp.status_code == 400


def test_landing_accepts_redeemed_invitation_within_redemption_window(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_client, status=PatientInvitation.Status.ISSUED)
    PatientInvitation.objects.filter(pk=inv.pk).update(
        status=PatientInvitation.Status.REDEEMED, last_updated=timezone.now() - timedelta(hours=1)
    )

    resp = Client().get(f"/patient/?code={code}")

    assert resp.status_code == 200


def test_codeless_visit_after_cancellation_is_rejected_and_session_cleared(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    resp = client.get(f"/patient/?code={code}")
    assert resp.status_code == 200
    assert SESSION_INVITATION_KEY in client.session

    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_client, status=PatientInvitation.Status.ISSUED)
    PatientInvitation.objects.filter(pk=inv.pk).update(status=PatientInvitation.Status.CANCELLED)

    resp2 = client.get("/patient/")

    assert resp2.status_code == 400
    assert SESSION_INVITATION_KEY not in client.session


def test_codeless_visit_after_reissue_with_old_session_is_rejected(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    resp = client.get(f"/patient/?code={code}")
    assert resp.status_code == 200

    PatientInvitation.build_link(pamela, ehr_client)  # mints a fresh invitation, marking the old one REISSUED

    resp2 = client.get("/patient/")

    assert resp2.status_code == 400


def test_valid_visit_caps_session_expiry_to_redemption_window(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()

    resp = client.get(f"/patient/?code={code}")

    assert resp.status_code == 200
    age = client.session.get_expiry_age()
    assert 0 < age <= 12 * 3600


def test_consent_post_rejects_expired_code_and_creates_no_consent(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_client, status=PatientInvitation.Status.ISSUED)
    PatientInvitation.objects.filter(pk=inv.pk).update(last_updated=timezone.now() - timedelta(days=8))

    resp = Client().post(f"/patient/consent/{ds.id}/", {"code": code})

    assert resp.status_code == 400
    study = Study.objects.get(name="Lifespan Study on BP & HR")
    star = CodeableConcept.objects.get(coding_system="http://hl7.org/fhir/resource-types", coding_code="*")
    study_patient = StudyPatient.objects.get(study=study, patient=pamela)
    assert not StudyPatientScopeConsent.objects.filter(study_patient=study_patient, scope_code=star).exists()


def test_done_without_session_is_invalid(db):
    call_command("seed", stdout=io.StringIO())

    resp = Client().get("/patient/done/")

    assert resp.status_code == 400


def test_done_lists_connected_source_after_consent(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    assert client.get(f"/patient/?code={code}").status_code == 200
    assert client.post(f"/patient/consent/{ds.id}/", {"code": code}).status_code == 302

    resp = client.get("/patient/done/")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "You're all set" in html
    assert "EHR Patient Portal" in html
    assert "Manage sharing" in html
    assert 'href="/patient/"' in html
    assert "pf-back" in html  # back link to the hub (§H)


def test_done_shows_only_the_just_connected_source(db):
    """Item G (amended): done() leads with -- and shows *only* -- the source consent() just
    recorded into the session, not every connected source. Pamela is already seeded with Oura
    fully consented (both its supported scopes, across her two studies), so it's a real
    "other connected source" this must hide once she also consents the EHR Patient Portal."""
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    client.get(f"/patient/?code={code}")

    # Confirm the premise against the actual seeded consent rows before asserting the hiding.
    assert any(s["name"] == "Oura" and s["connected"] for s in _sources(pamela))

    client.post(f"/patient/consent/{ds.id}/", {"code": code})

    resp = client.get("/patient/done/")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "EHR Patient Portal" in html
    assert "Oura" not in html


def test_done_shows_fhir_source_facility_and_record_count(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    client.get(f"/patient/?code={code}")
    client.post(f"/patient/consent/{ds.id}/", {"code": code})

    location = EhrBrandLocation.objects.get(name="Epic Sandbox - Madison Campus")
    fhir_source = FhirSource.objects.create(patient=pamela, data_source=ds, ehr_brand_location=location)
    for _ in range(3):
        FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Observation")

    resp = client.get("/patient/done/")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Epic Sandbox - Madison Campus" in html
    assert "3 records" in html


def test_manage_get_shows_consented_scopes(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    client.get(f"/patient/?code={code}")
    client.post(f"/patient/consent/{ds.id}/", {"code": code})

    resp = client.get(f"/patient/manage/{ds.id}/")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "You're sharing" in html
    assert "Clinical records" in html
    assert "Stop sharing" in html
    assert "pf-card__icon" in html and "pf-actions" in html
    assert "pf-back" in html and 'href="/patient/"' in html  # back link to the hub (§H)


def test_manage_shows_fhir_source_facility_and_record_count(db):
    """Item D extended to manage(): a source with a registered FhirSource shows the facility
    and record count there too, not just the bare scope label (§D amendment)."""
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    client.get(f"/patient/?code={code}")
    client.post(f"/patient/consent/{ds.id}/", {"code": code})

    location = EhrBrandLocation.objects.get(name="Epic Sandbox - Madison Campus")
    fhir_source = FhirSource.objects.create(patient=pamela, data_source=ds, ehr_brand_location=location)
    for _ in range(3):
        FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Observation")

    resp = client.get(f"/patient/manage/{ds.id}/")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Epic Sandbox - Madison Campus" in html
    assert "3 records" in html


def test_manage_get_rejects_source_with_nothing_consented(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    client.get(f"/patient/?code={code}")

    resp = client.get(f"/patient/manage/{ds.id}/")

    assert resp.status_code == 400


def test_consent_rejects_non_patient_facing_source(db):
    """A direct /patient/consent/<id>/ hit for a source with no patient-facing client (CareX,
    §J) must 400 -- consent() previously built its pending pairs straight off the DataSource,
    bypassing the same patient_facing gate _sources()/manage() already enforce, which let a
    crafted link consent to an excluded source."""
    call_command("seed", stdout=io.StringIO())
    peter = Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    carex_ds = DataSource.objects.get(name="CareX")
    code = _mint(peter, ehr_client)
    study = Study.objects.get(name="Lifespan Study on Sleep & BP")
    omh_bp = CodeableConcept.objects.get(coding_code="omh:blood-pressure:4.0")
    study_patient = StudyPatient.objects.get(study=study, patient=peter)

    get_resp = Client().get(f"/patient/consent/{carex_ds.id}/?code={code}")
    assert get_resp.status_code == 400

    post_resp = Client().post(f"/patient/consent/{carex_ds.id}/", {"code": code})
    assert post_resp.status_code == 400
    assert not StudyPatientScopeConsent.objects.filter(study_patient=study_patient, scope_code=omh_bp).exists()


def test_manage_rejects_consented_but_non_patient_facing_source(db):
    """CareX has no patient-facing client flow (§J), so a direct /patient/manage/<id>/ hit for
    it must 400 even though CareX is (partly) consented for Peter -- manage() must not surface
    a source _sources() itself excludes from the patient-facing hub."""
    call_command("seed", stdout=io.StringIO())
    peter = Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    carex_ds = DataSource.objects.get(name="CareX")
    code = _mint(peter, ehr_client)
    study = Study.objects.get(name="Lifespan Study on Sleep & BP")
    omh_bp = CodeableConcept.objects.get(coding_code="omh:blood-pressure:4.0")
    study_patient = StudyPatient.objects.get(study=study, patient=peter)
    StudyPatientScopeConsent.objects.create(
        study_patient=study_patient, scope_code=omh_bp, consented=True, consented_time=timezone.now()
    )

    resp = Client().get(f"/patient/manage/{carex_ds.id}/?code={code}")

    assert resp.status_code == 400


def test_done_leads_with_most_recently_consented_source_without_session_marker(db):
    """Item G: when the visiting session carries no usable last-connected-source marker (e.g.
    it only ever hit the hub, never the consent POST that sets it), done() must not fall back to
    "every connected source" -- it picks the one most recently consented, so Peter's seeded
    Oura consent (older) must not leak in alongside a freshly-consented EHR Patient Portal."""
    call_command("seed", stdout=io.StringIO())
    peter = Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(peter, ehr_client)
    consenting_client = Client()
    consenting_client.get(f"/patient/?code={code}")
    consenting_client.post(f"/patient/consent/{ds.id}/", {"code": code})

    fresh_code = _mint(peter, ehr_client)
    fresh_client = Client()
    fresh_client.get(f"/patient/?code={fresh_code}")

    resp = fresh_client.get("/patient/done/")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "EHR Patient Portal" in html
    assert "Oura" not in html


def test_manage_shows_single_card_for_fhir_source(db):
    """Item B: once a FhirSource is registered, manage() renders exactly one .pf-card (icon +
    source name + facility/record-count line) instead of one card per consented scope."""
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    client.get(f"/patient/?code={code}")
    client.post(f"/patient/consent/{ds.id}/", {"code": code})

    location = EhrBrandLocation.objects.get(name="Epic Sandbox - Madison Campus")
    fhir_source = FhirSource.objects.create(patient=pamela, data_source=ds, ehr_brand_location=location)
    FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Observation")

    resp = client.get(f"/patient/manage/{ds.id}/")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert html.count("pf-card__icon") == 1
    assert "Epic Sandbox - Madison Campus" in html


def test_done_and_manage_show_per_type_sync_receipt(db):
    """Items D & I: once a FhirSource has aux resources, both done() and manage() render a
    pf-receipt breakdown -- synced counts by type (desc, then label) plus a "Not synced"
    section for any type the EHR client's SMART scopes promise but nothing has landed for yet,
    and a closing total-synced row."""
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    client.get(f"/patient/?code={code}")
    client.post(f"/patient/consent/{ds.id}/", {"code": code})

    location = EhrBrandLocation.objects.get(name="Epic Sandbox - Madison Campus")
    fhir_source = FhirSource.objects.create(patient=pamela, data_source=ds, ehr_brand_location=location)
    FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Observation")
    FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Observation")
    FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Condition")

    for path in (f"/patient/manage/{ds.id}/", "/patient/done/"):
        resp = client.get(path)
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "Observations" in html
        assert "Conditions" in html
        assert "Not synced" in html
        assert "Devices" in html
        assert "Total synced" in html
        obs_row = html.split("Observations", 1)[1].split("</div>", 1)[0]
        assert "2" in obs_row
        cond_row = html.split("Conditions", 1)[1].split("</div>", 1)[0]
        assert "1" in cond_row
        total_row = html.split("Total synced", 1)[1].split("</div>", 1)[0]
        assert "3" in total_row


def test_manage_post_revokes_and_source_is_reconsentable(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    ds = DataSource.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)
    client = Client()
    client.get(f"/patient/?code={code}")
    client.post(f"/patient/consent/{ds.id}/", {"code": code})

    study = Study.objects.get(name="Lifespan Study on BP & HR")
    star = CodeableConcept.objects.get(coding_system="http://hl7.org/fhir/resource-types", coding_code="*")
    study_patient = StudyPatient.objects.get(study=study, patient=pamela)

    resp = client.post(f"/patient/manage/{ds.id}/", {"code": code})

    assert resp.status_code == 302
    assert resp.url == "/patient/"

    consent_row = StudyPatientScopeConsent.objects.get(study_patient=study_patient, scope_code=star)
    assert consent_row.consented is False

    landing = client.get("/patient/")
    card = _card_block(landing.content.decode(), "EHR Patient Portal")
    assert "Not consented" in card
    assert "pf-card__badge--on" not in card
    assert f"/patient/consent/{ds.id}/" in card

    consent_get = client.get(f"/patient/consent/{ds.id}/")
    assert consent_get.status_code == 200
    assert "Clinical records" in consent_get.content.decode()

    consent_post = client.post(f"/patient/consent/{ds.id}/", {})
    assert consent_post.status_code == 302
    consent_row.refresh_from_db()
    assert consent_row.consented is True

    landing2 = client.get("/patient/")
    card2 = _card_block(landing2.content.decode(), "EHR Patient Portal")
    assert "Consented" in card2
    assert "pf-card__badge--on" in card2
