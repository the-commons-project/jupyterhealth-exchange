"""Authorization tests for the OW wearable actions on PatientViewSet.

These tests are the regression suite for the IDOR fix in Stage B of the
OW integration hardening pass. Without the `_get_authorized_patient`
helper any authenticated user could trigger wearable_redirect /
wearable_status / revoke_consent for any patient.

The tests stub out OWIntegrationService entirely so we can run them
without a real Open Wearables backend.
"""

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from core.models import (
    JheSetting,
    JheUser,
    Organization,
    Patient,
    PatientOrganization,
    PractitionerOrganization,
    StudyPatient,
)

from .utils import Code, add_patient_to_study, add_patients, create_study


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ow_settings(db):
    """Configure the OW JheSettings the views need to read.

    JheSetting.value_string is cached in LocMemCache; clear before and
    after so cache leakage between tests doesn't produce false positives
    on the "missing credentials" test.
    """
    from django.core.cache import cache
    cache.clear()
    JheSetting.objects.update_or_create(
        key="ow.api_base_url",
        defaults={"value_type": "string", "value_string": "http://ow.test"},
    )
    JheSetting.objects.update_or_create(
        key="ow.api_key",
        defaults={"value_type": "string", "value_string": "test-key"},
    )
    yield
    cache.clear()


@pytest.fixture
def org_a(db):
    return Organization.objects.create(name="Org A", type="other")


@pytest.fixture
def org_b(db):
    return Organization.objects.create(name="Org B", type="other")


@pytest.fixture
def practitioner_org_a(org_a):
    """Practitioner who is a manager of Org A only."""
    user = JheUser.objects.create_user(
        email="prac-a@example.org",
        password="x",
        user_type="practitioner",
    )
    PractitionerOrganization.objects.create(
        practitioner=user.practitioner,
        organization=org_a,
        role="manager",
    )
    return user


@pytest.fixture
def practitioner_org_b(org_b):
    """Practitioner who is a manager of Org B only."""
    user = JheUser.objects.create_user(
        email="prac-b@example.org",
        password="x",
        user_type="practitioner",
    )
    PractitionerOrganization.objects.create(
        practitioner=user.practitioner,
        organization=org_b,
        role="manager",
    )
    return user


@pytest.fixture
def patient_in_org_a(org_a):
    """Patient enrolled in Org A."""
    user = JheUser.objects.create_user(
        email="pat-a@example.org",
        password="x",
        user_type="patient",
    )
    PatientOrganization.objects.create(patient=user.patient, organization=org_a)
    return user.patient


@pytest.fixture
def study_in_org_a(org_a, patient_in_org_a):
    """Study in Org A with the patient enrolled."""
    study = create_study(organization=org_a, codes=[Code.HeartRate])
    add_patient_to_study(patient=patient_in_org_a, study=study, consent=True)
    return study


# ---------------------------------------------------------------------------
# wearable_redirect — IDOR regression
# ---------------------------------------------------------------------------


def test_wearable_redirect_unauthenticated_returns_401(patient_in_org_a):
    """No auth → 401, never reaches OW."""
    client = APIClient()
    resp = client.post(
        f"/api/v1/patients/{patient_in_org_a.id}/wearable-redirect",
        data={"study_id": 1, "data_source_id": 1},
        format="json",
    )
    assert resp.status_code == 401


@patch("core.views.patient.ow_service")
def test_wearable_redirect_practitioner_in_other_org_is_forbidden(
    mock_ow, practitioner_org_b, patient_in_org_a, study_in_org_a, ow_settings
):
    """A practitioner in Org B cannot trigger wearable_redirect for a patient in Org A.

    This is the IDOR regression test. Without the fix in Stage B,
    practitioner_org_b would receive a 200 response and an authorization
    URL — letting them hijack the patient's OW user record.
    """
    client = APIClient()
    client.force_authenticate(practitioner_org_b)
    resp = client.post(
        f"/api/v1/patients/{patient_in_org_a.id}/wearable-redirect",
        data={"study_id": study_in_org_a.id, "data_source_id": 1},
        format="json",
    )
    assert resp.status_code == 403
    mock_ow.find_or_create_user.assert_not_called()


@patch("core.views.patient.ow_service")
def test_wearable_redirect_other_patient_is_forbidden(
    mock_ow, patient_in_org_a, ow_settings
):
    """A patient cannot trigger wearable_redirect for a different patient."""
    other_patient_user = JheUser.objects.create_user(
        email="other@example.org",
        password="x",
        user_type="patient",
    )
    client = APIClient()
    client.force_authenticate(other_patient_user)
    resp = client.post(
        f"/api/v1/patients/{patient_in_org_a.id}/wearable-redirect",
        data={"study_id": 1, "data_source_id": 1},
        format="json",
    )
    assert resp.status_code == 403
    mock_ow.find_or_create_user.assert_not_called()


@patch("core.views.patient.ow_service")
def test_wearable_redirect_authorized_practitioner_succeeds(
    mock_ow, practitioner_org_a, patient_in_org_a, study_in_org_a, ow_settings, device
):
    """A practitioner in the patient's org can trigger wearable_redirect."""
    mock_ow.find_or_create_user.return_value = "ow-user-uuid"
    mock_ow.get_wearable_auth_url.return_value = "https://provider.example.org/oauth/authorize?..."

    client = APIClient()
    client.force_authenticate(practitioner_org_a)
    resp = client.post(
        f"/api/v1/patients/{patient_in_org_a.id}/wearable-redirect",
        data={"study_id": study_in_org_a.id, "data_source_id": device.id},
        format="json",
    )
    assert resp.status_code == 200
    assert "authorization_url" in resp.json() or "authorizationUrl" in resp.json()


@patch("core.views.patient.ow_service")
def test_wearable_redirect_patient_self_succeeds(
    mock_ow, patient_in_org_a, study_in_org_a, ow_settings, device
):
    """A patient can trigger wearable_redirect for their own record."""
    mock_ow.find_or_create_user.return_value = "ow-user-uuid"
    mock_ow.get_wearable_auth_url.return_value = "https://provider.example.org/oauth/authorize"

    client = APIClient()
    client.force_authenticate(patient_in_org_a.jhe_user)
    resp = client.post(
        f"/api/v1/patients/{patient_in_org_a.id}/wearable-redirect",
        data={"study_id": study_in_org_a.id, "data_source_id": device.id},
        format="json",
    )
    assert resp.status_code == 200


@patch("core.views.patient.ow_service")
def test_wearable_redirect_passes_patient_details_to_ow(
    mock_ow, patient_in_org_a, study_in_org_a, ow_settings, device
):
    """OW user creation includes first_name, last_name, and external_user_id
    (JHE patient ID) for correlation in the OW dashboard."""
    patient_in_org_a.name_given = "Ada"
    patient_in_org_a.name_family = "Lovelace"
    patient_in_org_a.save()
    mock_ow.find_or_create_user.return_value = "ow-user-uuid"
    mock_ow.get_wearable_auth_url.return_value = "https://provider.example.org/oauth/authorize"

    client = APIClient()
    client.force_authenticate(patient_in_org_a.jhe_user)
    resp = client.post(
        f"/api/v1/patients/{patient_in_org_a.id}/wearable-redirect",
        data={"study_id": study_in_org_a.id, "data_source_id": device.id},
        format="json",
    )
    assert resp.status_code == 200
    mock_ow.find_or_create_user.assert_called_once_with(
        patient_in_org_a.jhe_user.email,
        first_name="Ada",
        last_name="Lovelace",
        external_user_id=str(patient_in_org_a.id),
    )


@patch("core.views.patient.ow_service")
def test_wearable_redirect_patient_not_in_study_returns_404(
    mock_ow, patient_in_org_a, ow_settings, device
):
    """If the patient isn't enrolled in the requested study, 404 — not silent success."""
    other_org = Organization.objects.create(name="Other", type="other")
    other_study = create_study(organization=other_org, codes=[Code.HeartRate])

    client = APIClient()
    client.force_authenticate(patient_in_org_a.jhe_user)
    resp = client.post(
        f"/api/v1/patients/{patient_in_org_a.id}/wearable-redirect",
        data={"study_id": other_study.id, "data_source_id": device.id},
        format="json",
    )
    assert resp.status_code == 404
    mock_ow.find_or_create_user.assert_not_called()



# ---------------------------------------------------------------------------
# wearable_status — IDOR regression
# ---------------------------------------------------------------------------


def test_wearable_status_practitioner_in_other_org_is_forbidden(
    practitioner_org_b, patient_in_org_a
):
    client = APIClient()
    client.force_authenticate(practitioner_org_b)
    resp = client.get(f"/api/v1/patients/{patient_in_org_a.id}/wearable-status")
    assert resp.status_code == 403


def test_wearable_status_patient_self_succeeds(patient_in_org_a):
    client = APIClient()
    client.force_authenticate(patient_in_org_a.jhe_user)
    resp = client.get(f"/api/v1/patients/{patient_in_org_a.id}/wearable-status")
    # 200 with connected: false because the patient has no ow:* identifier
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("connected") is False


# ---------------------------------------------------------------------------
# revoke_consent — IDOR regression
# ---------------------------------------------------------------------------


def test_revoke_consent_practitioner_in_other_org_is_forbidden(
    practitioner_org_b, patient_in_org_a, study_in_org_a
):
    client = APIClient()
    client.force_authenticate(practitioner_org_b)
    resp = client.delete(
        f"/api/v1/patients/{patient_in_org_a.id}/consents/{study_in_org_a.id}"
    )
    assert resp.status_code == 403


def test_revoke_consent_patient_not_in_study_returns_404(patient_in_org_a):
    other_org = Organization.objects.create(name="Other", type="other")
    other_study = create_study(organization=other_org, codes=[Code.HeartRate])
    client = APIClient()
    client.force_authenticate(patient_in_org_a.jhe_user)
    resp = client.delete(f"/api/v1/patients/{patient_in_org_a.id}/consents/{other_study.id}")
    assert resp.status_code == 404


@patch("core.views.patient.ow_service")
def test_revoke_consent_patient_self_succeeds(
    mock_ow, patient_in_org_a, study_in_org_a
):
    client = APIClient()
    client.force_authenticate(patient_in_org_a.jhe_user)
    resp = client.delete(
        f"/api/v1/patients/{patient_in_org_a.id}/consents/{study_in_org_a.id}"
    )
    assert resp.status_code == 200
    assert resp.json().get("status") == "revoked"


@patch("core.views.patient.ow_service")
def test_revoke_consent_calls_revoke_connection_for_ow_patient(
    mock_ow, patient_in_org_a, study_in_org_a
):
    """D9: revoking consent also revokes the OW vendor connection so OW
    stops pulling from Oura. Requires the patient to have an ow:* identifier
    — if they don't, the OW call is skipped (no-op is fine).
    """
    # Give the patient an ow: identifier so the revoke path fires.
    patient_in_org_a.jhe_user.identifier = "ow:d9-test-uuid"
    patient_in_org_a.jhe_user.save()

    client = APIClient()
    client.force_authenticate(patient_in_org_a.jhe_user)
    resp = client.delete(
        f"/api/v1/patients/{patient_in_org_a.id}/consents/{study_in_org_a.id}"
    )
    assert resp.status_code == 200
    mock_ow.revoke_connection.assert_called_once_with("d9-test-uuid", provider="oura")


