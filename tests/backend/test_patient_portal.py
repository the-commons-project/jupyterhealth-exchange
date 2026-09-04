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
from core.views.patient_portal import SESSION_KEY, _invitation_from_code, _sources

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
    assert "Not connected" in html
    assert "pf-card__badge" in html
    assert f"/patient/consent/{ds.id}/?code={code}" in html

    card = _card_block(html, "EHR Patient Portal")
    assert "Not connected" in card
    assert "pf-card__badge--on" not in card


def test_session_remembers_patient_for_a_codeless_visit(db):
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    code = _mint(pamela, ehr_client)

    client = Client()
    resp = client.get(f"/patient/?code={code}")

    assert resp.status_code == 200
    assert client.session[SESSION_KEY] == pamela.id

    resp2 = client.get("/patient/")

    assert resp2.status_code == 200
    assert "EHR Patient Portal" in resp2.content.decode()


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
    assert "Connected" in card
    assert "pf-card__badge--on" in card
    assert f"/patient/manage/{ds.id}/?code={code}" in card


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
    # Connected here; verified against the actual seeded consent rows, not assumed.
    assert "pf-card__badge--on" in card
    assert "Connected" in card


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
    assert "pf-consent-row" in html


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


def test_consent_post_cross_client_mints_carex_invitation(db):
    call_command("seed", stdout=io.StringIO())
    peter = Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    carex_client = Application.objects.get(name="CareX")
    carex_ds = DataSource.objects.get(name="CareX")
    code = _mint(peter, ehr_client)

    resp = Client().post(f"/patient/consent/{carex_ds.id}/", {"code": code})

    assert resp.status_code == 302
    assert resp.url.startswith("https://carex.ai/invitation/")

    inv = PatientInvitation.objects.get(patient=peter, client=carex_client)
    assert inv.status == PatientInvitation.Status.ISSUED

    study = Study.objects.get(name="Lifespan Study on Sleep & BP")
    study_patient = StudyPatient.objects.get(study=study, patient=peter)
    bp_code = CodeableConcept.objects.get(coding_code="omh:blood-pressure:4.0")
    consent_row = StudyPatientScopeConsent.objects.get(study_patient=study_patient, scope_code=bp_code)
    assert consent_row.consented is True

    source = next(s for s in _sources(peter) if s["id"] == carex_ds.id)
    assert source["pending"] == []


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
    assert SESSION_KEY in client.session

    inv = PatientInvitation.objects.get(patient=pamela, client=ehr_client, status=PatientInvitation.Status.ISSUED)
    PatientInvitation.objects.filter(pk=inv.pk).update(status=PatientInvitation.Status.CANCELLED)

    resp2 = client.get("/patient/")

    assert resp2.status_code == 400
    assert SESSION_KEY not in client.session


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
    assert "Not connected" in card
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
    assert "Connected" in card2
    assert "pf-card__badge--on" in card2
