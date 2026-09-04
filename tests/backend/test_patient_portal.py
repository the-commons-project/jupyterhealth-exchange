import io

from django.core.management import call_command

from core.models import CodeableConcept, DataSource, JheClient, Patient, Study, StudyDataSource, StudyScopeRequest


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
