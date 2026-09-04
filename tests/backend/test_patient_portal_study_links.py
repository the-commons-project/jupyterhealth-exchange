"""Two rules the hub, consent and manage screens share (found during manual testing 2026-09-04):

* a source only carries the scopes of studies that collect through it (StudyDataSource link),
  so consenting/revoking Oura never touches a scope another study requests via CareX;
* a revoked source's card stops advertising the facility and record count -- Not consented
  reads as scopes only, even though the FhirSource and its imported records are retained.
"""

import io

from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from oauth2_provider.models import Application

from core.models import (
    CodeableConcept,
    DataSource,
    EhrBrandLocation,
    FhirAuxResource,
    FhirSource,
    Patient,
    StudyPatientScopeConsent,
)
from tests.backend.test_patient_portal import _card_block, _mint


def _seed_pamela():
    call_command("seed", stdout=io.StringIO())
    pamela = Patient.objects.get(jhe_user__email="ll_patient_pamela@example.com")
    ehr_client = Application.objects.get(name="EHR Patient Portal")
    return pamela, _mint(pamela, ehr_client)


def test_revoking_oura_leaves_heart_rate_requested_via_carex_consented(db):
    pamela, code = _seed_pamela()
    oura_ds = DataSource.objects.get(name="Oura")
    hr_code = CodeableConcept.objects.get(coding_code="omh:heart-rate:2.0")
    sleep_code = CodeableConcept.objects.get(coding_code="ieee:sleep-episode:1.0")
    assert StudyPatientScopeConsent.objects.filter(study_patient__patient=pamela, scope_code=hr_code, consented=True).exists()

    client = Client()
    client.get(f"/patient/?code={code}")
    manage = client.get(f"/patient/manage/{oura_ds.id}/?code={code}")
    assert manage.status_code == 200
    assert "Sleep episode" in manage.content.decode()
    assert "Heart Rate" not in manage.content.decode()

    resp = client.post(f"/patient/manage/{oura_ds.id}/", {"code": code})
    assert resp.status_code == 302

    rows = StudyPatientScopeConsent.objects.filter(study_patient__patient=pamela)
    assert not rows.filter(scope_code=sleep_code, consented=True).exists()
    assert rows.filter(scope_code=hr_code, consented=True).exists()  # BP & HR collects it via CareX, untouched


def test_consenting_oura_records_only_the_sleep_study_scope(db):
    pamela, code = _seed_pamela()
    oura_ds = DataSource.objects.get(name="Oura")
    hr_code = CodeableConcept.objects.get(coding_code="omh:heart-rate:2.0")
    sleep_code = CodeableConcept.objects.get(coding_code="ieee:sleep-episode:1.0")
    StudyPatientScopeConsent.objects.filter(study_patient__patient=pamela, scope_code__in=[hr_code, sleep_code]).update(
        consented=False, consented_time=timezone.now()
    )

    client = Client()
    client.get(f"/patient/?code={code}")
    resp = client.post(f"/patient/consent/{oura_ds.id}/", {"code": code})
    assert resp.status_code == 302

    rows = StudyPatientScopeConsent.objects.filter(study_patient__patient=pamela)
    assert rows.filter(scope_code=sleep_code, consented=True).exists()
    assert not rows.filter(scope_code=hr_code, consented=True).exists()


def test_revoked_ehr_card_hides_facility_and_record_count(db):
    pamela, code = _seed_pamela()
    ds = DataSource.objects.get(name="EHR Patient Portal")
    client = Client()
    client.get(f"/patient/?code={code}")
    client.post(f"/patient/consent/{ds.id}/", {"code": code})

    location = EhrBrandLocation.objects.get(name="Epic Sandbox - Madison Campus")
    fhir_source = FhirSource.objects.create(patient=pamela, data_source=ds, ehr_brand_location=location)
    for _ in range(3):
        FhirAuxResource.objects.create(fhir_source=fhir_source, resource_type="Observation")

    card = _card_block(client.get(f"/patient/?code={code}").content.decode(), "EHR Patient Portal")
    assert "Consented" in card and "Epic Sandbox - Madison Campus" in card and "3 records" in card

    assert client.post(f"/patient/manage/{ds.id}/", {"code": code}).status_code == 302

    card = _card_block(client.get(f"/patient/?code={code}").content.decode(), "EHR Patient Portal")
    assert "Not consented" in card
    assert "Clinical records" in card
    assert "Epic Sandbox - Madison Campus" not in card and "records" not in card.replace("Clinical records", "")
    assert FhirSource.objects.filter(patient=pamela, data_source=ds).exists()  # retained, just not advertised
