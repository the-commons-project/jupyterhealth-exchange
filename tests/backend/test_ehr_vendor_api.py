"""Tests for the EHRs admin API: /api/v1/ehr_vendors, superuser-only like /api/v1/practitioners.

EhrVendor is the only thing this API lets an admin edit -- EhrBrand/EhrBrandLocation are
read-only, nested under their vendor for display (managed by manage.py import_ehr_brands and
the plain Django admin instead).
"""

import pytest
from rest_framework.test import APIClient

from core.models import EhrBrand, EhrBrandLocation, EhrVendor

URL = "/api/v1/ehr_vendors"


@pytest.fixture
def superuser_client(superuser):
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(superuser)
    return client


@pytest.fixture
def practitioner_client(user):
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(user)
    return client


@pytest.fixture
def patient_client(patient):
    client = APIClient()
    client.default_format = "json"
    client.force_authenticate(patient.jhe_user)
    return client


@pytest.fixture
def vendor(db):
    v = EhrVendor.objects.create(
        name="Epic",
        ehr_client_id="epic-client-id",
        supported_scopes="patient/Patient.read patient/Condition.read",
    )
    brand = EhrBrand.objects.create(name="Mount Sinai", vendor=v, fhir_base_url="https://sinai.example.org/FHIR/R4")
    EhrBrandLocation.objects.create(
        brand=brand, name="Mount Sinai Hospital", address_text="1 Levy Pl, New York, NY", state="NY"
    )
    EhrBrandLocation.objects.create(brand=brand, name="Mount Sinai West", address_text="1000 10th Ave", state="NY")
    return v


def test_superuser_can_list(superuser_client, vendor):
    r = superuser_client.get(URL)
    assert r.status_code == 200
    row = next(row for row in r.json()["results"] if row["id"] == vendor.id)
    assert row["name"] == "Epic"
    assert row["ehrClientId"] == "epic-client-id"


def test_superuser_can_retrieve_with_brands_and_locations(superuser_client, vendor):
    r = superuser_client.get(f"{URL}/{vendor.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["supportedScopes"] == "patient/Patient.read patient/Condition.read"
    assert len(body["brands"]) == 1
    brand = body["brands"][0]
    assert brand["name"] == "Mount Sinai"
    names = {loc["name"] for loc in brand["locations"]}
    assert names == {"Mount Sinai Hospital", "Mount Sinai West"}


def test_superuser_can_create(superuser_client):
    r = superuser_client.post(URL, {"name": "Cerner", "ehrClientId": "cerner-client-id"})
    assert r.status_code == 201, r.text
    assert EhrVendor.objects.filter(name="Cerner", ehr_client_id="cerner-client-id").exists()


def test_superuser_can_update(superuser_client, vendor):
    r = superuser_client.patch(f"{URL}/{vendor.id}", {"ehrClientId": "rotated-client-id"})
    assert r.status_code == 200, r.text
    vendor.refresh_from_db()
    assert vendor.ehr_client_id == "rotated-client-id"


def test_superuser_delete_is_blocked_while_brands_exist(superuser_client, vendor):
    r = superuser_client.delete(f"{URL}/{vendor.id}")
    assert r.status_code == 400
    assert EhrVendor.objects.filter(id=vendor.id).exists()


def test_superuser_can_delete_a_vendor_with_no_brands(superuser_client):
    v = EhrVendor.objects.create(name="Unused Vendor")
    r = superuser_client.delete(f"{URL}/{v.id}")
    assert r.status_code == 204
    assert not EhrVendor.objects.filter(id=v.id).exists()


def test_practitioner_is_denied(practitioner_client, vendor):
    assert practitioner_client.get(URL).status_code == 403
    assert practitioner_client.get(f"{URL}/{vendor.id}").status_code == 403
    assert practitioner_client.post(URL, {"name": "X"}).status_code == 403
    assert practitioner_client.delete(f"{URL}/{vendor.id}").status_code == 403


def test_patient_is_denied(patient_client, vendor):
    assert patient_client.get(URL).status_code == 403


def test_anonymous_is_401(db, vendor):
    assert APIClient().get(URL).status_code == 401
