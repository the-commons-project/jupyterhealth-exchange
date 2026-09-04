import io

import pytest
from django.core.management import call_command
from django.test import Client
from oauth2_provider.models import get_application_model

from core.models import (
    CodeableConcept,
    DataSource,
    JheClient,
    Patient,
    PatientInvitation,
    Study,
    StudyDataSource,
    StudyScopeRequest,
)
from core.views.patient_portal import _invitation_from_code

Application = get_application_model()


def _mint(patient, client):
    """Mint an invitation exactly as the API does, returning the wire-format code (the part
    of the link after `code=`, still percent-encoded -- e.g. "localhost%3A8001_<token>")."""
    _invitation, link = PatientInvitation.build_link(patient, client)
    return link.split("code=", 1)[1]


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
