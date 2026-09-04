import io

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from oauth2_provider.models import get_application_model

from core.models import (
    CodeableConcept,
    DataSource,
    JheClient,
    Patient,
    PatientInvitation,
    Study,
    StudyDataSource,
    StudyPatient,
    StudyPatientScopeConsent,
    StudyScopeRequest,
)
from core.views.patient_portal import SESSION_KEY, _invitation_from_code

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
