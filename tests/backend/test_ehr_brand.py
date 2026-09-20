"""Tests for the EHR brand / vendor / location models (hospital branding + selection)."""

import pytest
from django.db import IntegrityError
from django.db.models import ProtectedError

from core.models import EhrBrand, EhrBrandLocation, EhrVendor


@pytest.fixture
def epic(db):
    return EhrVendor.objects.create(name="Epic")


def test_create_brand_with_locations(epic):
    brand = EhrBrand.objects.create(
        name="Mount Sinai",
        vendor=epic,
        fhir_base_url="https://fhir.mountsinai.org/api/FHIR/R4",
    )
    EhrBrandLocation.objects.create(
        brand=brand,
        name="Mount Sinai Hospital",
        address_text="1 Gustave L. Levy Pl, New York, NY 10029",
        city="New York",
        state="NY",
        postal_code="10029",
    )
    EhrBrandLocation.objects.create(
        brand=brand,
        name="Mount Sinai West",
        address_text="1000 10th Ave, New York, NY 10019",
        city="New York",
        state="NY",
        postal_code="10019",
    )
    assert brand.locations.count() == 2
    assert brand.locations.first().brand_id == brand.id


def test_fhir_base_url_is_unique(epic):
    EhrBrand.objects.create(name="A", vendor=epic, fhir_base_url="https://ehr.example.org/FHIR/R4")
    with pytest.raises(IntegrityError):
        EhrBrand.objects.create(name="B", vendor=epic, fhir_base_url="https://ehr.example.org/FHIR/R4")


def test_vendor_is_required(db):
    with pytest.raises(IntegrityError):
        EhrBrand.objects.create(name="No Vendor", fhir_base_url="https://x.example.org/FHIR/R4")


def test_many_brands_share_one_vendor(epic):
    a = EhrBrand.objects.create(name="A", vendor=epic, fhir_base_url="https://a.example.org/FHIR/R4")
    b = EhrBrand.objects.create(name="B", vendor=epic, fhir_base_url="https://b.example.org/FHIR/R4")
    assert set(epic.brands.all()) == {a, b}


def test_deleting_brand_cascades_to_locations(epic):
    brand = EhrBrand.objects.create(name="Cascade", vendor=epic, fhir_base_url="https://c.example.org/FHIR/R4")
    EhrBrandLocation.objects.create(brand=brand, name="Site 1", address_text="123 St")
    brand.delete()
    assert EhrBrandLocation.objects.count() == 0


def test_deleting_a_vendor_with_brands_is_protected(epic):
    EhrBrand.objects.create(name="Cascade", vendor=epic, fhir_base_url="https://c2.example.org/FHIR/R4")
    with pytest.raises(ProtectedError):
        epic.delete()
