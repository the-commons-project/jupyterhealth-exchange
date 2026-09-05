"""Tests for the EHR Patient Portal client: identifier proxy + page routes + seeded config.

The client is served at /clients/ehr-patient-portal/, matching its Application and DataSource
rows. That path is registered as a redirect URI on the Epic app, so it and the Epic-side
registration have to move together. Its /api/v1/ endpoints moved with it.
"""

import re

import pytest
from django.template.loader import render_to_string
from oauth2_provider.models import get_application_model
from rest_framework.test import APIClient

from core.models import ClientDataSource, DataSource, JheClient, JheUser, PatientIdentifier


def _rail_step_classes(html):
    return re.findall(r'<li class="pf-rail__step([^"]*)"', html)


def _error_wrap(html):
    """(callout, actions) of the #pf_error_wrap block: the buttons are a sibling of the red .pf-error box."""
    wrap = html.split('id="pf_error_wrap"', 1)[1]
    callout, actions = wrap.split('<div class="pf-actions"', 1)
    return callout, actions.split("</div>", 1)[0]


@pytest.fixture
def patient_client(patient):
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(patient.jhe_user)
    return client


def test_save_identifier_creates_patient_identifier(patient, patient_client):
    resp = patient_client.post(
        "/api/v1/ehr-patient-portal/identifier",
        {"system": "https://fhir.epic.com/.../R4", "value": "epic-abc-123"},
    )
    assert resp.status_code == 200
    assert PatientIdentifier.objects.filter(
        patient=patient, system="https://fhir.epic.com/.../R4", value="epic-abc-123"
    ).exists()


def test_save_identifier_is_idempotent(patient, patient_client):
    payload = {"system": "sys", "value": "v1"}
    patient_client.post("/api/v1/ehr-patient-portal/identifier", payload)
    patient_client.post("/api/v1/ehr-patient-portal/identifier", payload)
    assert PatientIdentifier.objects.filter(patient=patient, system="sys", value="v1").count() == 1


def test_save_identifier_does_not_clobber_other_identifiers(patient, patient_client):
    PatientIdentifier.objects.create(patient=patient, system="existing", value="keepme")
    patient_client.post("/api/v1/ehr-patient-portal/identifier", {"system": "sys", "value": "v1"})
    assert PatientIdentifier.objects.filter(patient=patient, system="existing", value="keepme").exists()


def test_save_identifier_conflicts_when_owned_by_another_patient(organization, patient, patient_client):
    # (system, value) is globally unique. Without the conflict check the caller gets a 200
    # and believes the id was attached to them, while the row still points at someone else.
    other = JheUser.objects.create_user(
        email="other-patient@example.org",
        password="testpass123",
        identifier="other-patient",
        user_type="patient",
    ).patient
    other.organizations.add(organization)
    PatientIdentifier.objects.create(patient=other, system="sys", value="taken")

    resp = patient_client.post("/api/v1/ehr-patient-portal/identifier", {"system": "sys", "value": "taken"})

    assert resp.status_code == 409
    assert PatientIdentifier.objects.get(system="sys", value="taken").patient_id == other.id
    assert not PatientIdentifier.objects.filter(patient=patient, system="sys", value="taken").exists()


def test_save_identifier_requires_patient(db, user):
    # `user` fixture is a practitioner (no patient profile) -> 400
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(user)
    resp = client.post("/api/v1/ehr-patient-portal/identifier", {"system": "sys", "value": "v1"})
    assert resp.status_code == 400


def test_save_identifier_validates_body(patient, patient_client):
    resp = patient_client.post("/api/v1/ehr-patient-portal/identifier", {"system": "sys"})
    assert resp.status_code == 400


def test_connect_page_renders(db, client):
    # AllowAny page; renders even unauthenticated (JS handles auth).
    # `db` is needed because the view reads Patient Access aux_data from the DB.
    resp = client.get("/clients/ehr-patient-portal/")
    assert resp.status_code == 200


def test_callback_page_renders(db, client):
    resp = client.get("/clients/ehr-patient-portal/callback")
    assert resp.status_code == 200


def test_connect_page_includes_hospital_picker(db, client):
    # The connect page must render the picker input and pass it to startEhrPatientPortalConnect,
    # otherwise the patient can never choose a hospital.
    html = client.get("/clients/ehr-patient-portal/").content.decode()
    assert 'id="hospital-search"' in html
    assert 'id="hospital-results"' in html
    assert "startEhrPatientPortalConnect(out, EHR_PATIENT_PORTAL_CONFIG, picker)" in html


def test_connect_page_is_branded_and_preserves_js_hooks(db, client):
    html = client.get("/clients/ehr-patient-portal/").content.decode()

    assert "EHR_PATIENT_PORTAL_CONFIG" in html and "showFlowError" in html
    assert '<pre id="out" hidden>' in html  # the flow log stays in the DOM for the JS; errors surface via pf_error
    assert html.index('id="hospital-picker"') < html.index('id="out"')
    search = html.split('class="pf-search"', 1)[1]
    assert search.index('id="hospital-search"') < search.index('id="hospital-results"')
    assert "Share your medical records" in html and "We only sync the records you approve" in html
    steps = _rail_step_classes(html)
    assert len(steps) == 3 and "is-active" in steps[0]
    assert "pf-back" in html
    assert "hidden" in html.split('id="pf_error_wrap"', 1)[1].split(">", 1)[0]
    assert "We couldn't process your invitation" in html
    callout, actions = _error_wrap(html)
    assert "pf-btn" not in callout
    assert "Try again" in actions and "Back" in actions


def test_callback_page_frames_the_import_and_auto_advances(db, client):
    html = client.get("/clients/ehr-patient-portal/callback").content.decode()

    assert 'class="pf-page"' in html and 'id="out"' in html
    assert "Importing your records" in html and "Securely syncing your records into your study" in html
    assert "pf-import-card" in html and "pf-progress" in html and "Syncing your records" in html
    steps = _rail_step_classes(html)
    assert len(steps) == 3 and "is-active" in steps[2]
    # No exits while the import runs: no View summary link and no Back link, so a partial receipt can't be captured.
    assert "View summary" not in html and "pf-back" not in html
    assert "We couldn't reach your healthcare organization" in html
    callout, actions = _error_wrap(html)
    assert "pf-btn" not in callout
    assert 'href="/clients/ehr-patient-portal/"' in actions and "Choose a different organization" in actions
    script = html.split("finishEhrPatientPortalConnect", 1)[1]
    assert "showFlowError" in script
    assert "could not fetch" in script and "saved " in script  # an all-types-failed run is an error, not a success


def test_progress_rail_marks_active_and_done_steps():
    html = render_to_string("clients/ehr-patient-portal/progress_rail.html", {"active_step": 2})

    steps = _rail_step_classes(html)
    assert len(steps) == 3
    assert "is-done" in steps[0] and "is-active" not in steps[0]
    assert "is-active" in steps[1] and "is-done" not in steps[1]
    assert "is-done" not in steps[2] and "is-active" not in steps[2]
    for label in ("Choose organization", "Sign in", "Import records"):
        assert label in html


def test_connect_page_data_source_comes_from_the_client_data_source_link(db, client):
    # The view must read the data source off the client's ClientDataSource row. It used to
    # match DataSource by a hardcoded name at request time, which let the client name and the
    # data source name drift apart unnoticed.
    app = get_application_model().objects.create(name="EHR Patient Portal", client_id="local-app-id")
    JheClient.objects.create(application=app, aux_data={"client_id": "epic-id", "scopes": "openid"})
    linked = DataSource.objects.create(name="EHR Patient Portal", type="patient_app")
    ClientDataSource.objects.create(client=app, data_source=linked)

    html = client.get("/clients/ehr-patient-portal/").content.decode()

    assert f'dataSourceId: "{linked.id}"' in html


def test_connect_page_has_no_data_source_when_the_link_is_missing(db, client):
    # An unlinked client yields an empty id rather than silently latching onto some other
    # row that happens to share the name -- linking is an explicit seed/admin action.
    app = get_application_model().objects.create(name="EHR Patient Portal", client_id="local-app-id")
    JheClient.objects.create(application=app, aux_data={"client_id": "epic-id", "scopes": "openid"})
    DataSource.objects.create(name="EHR Patient Portal", type="patient_app")

    html = client.get("/clients/ehr-patient-portal/").content.decode()

    assert 'dataSourceId: ""' in html


def test_rename_migration_renames_both_rows_and_links_them(db):
    # Migration 0046: the deployed rows were named "Patient Access" / "Patient Access API",
    # which collided with the unrelated auth.patient_access_clients login-mode setting.
    import importlib

    from django.apps import apps as django_apps

    migration = importlib.import_module("core.migrations.0046_rename_ehr_patient_portal")

    app = get_application_model().objects.create(name="Patient Access", client_id="deployed-app-id")
    data_source = DataSource.objects.create(name="Patient Access API", type="medical_device")

    migration.rename_to_ehr_patient_portal(django_apps, None)

    app.refresh_from_db()
    data_source.refresh_from_db()
    assert app.name == "EHR Patient Portal"
    assert data_source.name == "EHR Patient Portal"
    assert data_source.type == "patient_app"
    # Renamed in place, so anything referencing them by FK still resolves.
    assert app.client_id == "deployed-app-id"
    assert ClientDataSource.objects.filter(client=app, data_source=data_source).exists()


def test_rename_migration_is_idempotent(db):
    # Replaying it (or running it on a database seeded after the rename shipped) must not
    # produce a second row under either name -- neither name column is unique.
    import importlib

    from django.apps import apps as django_apps

    migration = importlib.import_module("core.migrations.0046_rename_ehr_patient_portal")

    get_application_model().objects.create(name="Patient Access", client_id="deployed-app-id")
    DataSource.objects.create(name="Patient Access API", type="medical_device")

    migration.rename_to_ehr_patient_portal(django_apps, None)
    migration.rename_to_ehr_patient_portal(django_apps, None)

    assert get_application_model().objects.filter(name="EHR Patient Portal").count() == 1
    assert DataSource.objects.filter(name="EHR Patient Portal").count() == 1
    assert ClientDataSource.objects.count() == 1
