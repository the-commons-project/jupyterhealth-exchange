"""Tests for the EHR brands importer (SMART user-access Brands Bundle -> DB)."""

import json
from pathlib import Path

from django.core.management import call_command

from core.management.commands.import_ehr_brands import DEFAULT_SAMPLE, import_brands_bundle
from core.models import EhrBrand, EhrBrandLocation, EhrVendor


def _bundle():
    """A minimal SMART user-access Brands Bundle: 1 brand, 1 endpoint, 2 facilities."""
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {
                "fullUrl": "https://example.org/Endpoint/ep1",
                "resource": {
                    "resourceType": "Endpoint",
                    "id": "ep1",
                    "address": "https://fhir.mountsinai.org/api/FHIR/R4",
                    "connectionType": {"code": "hl7-fhir-rest"},
                },
            },
            {
                "resource": {
                    "resourceType": "Organization",
                    "id": "brand-msinai",
                    "name": "Mount Sinai",
                    "identifier": [{"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567890"}],
                    "endpoint": [{"reference": "Endpoint/ep1"}],
                },
            },
            {
                "resource": {
                    "resourceType": "Organization",
                    "id": "loc-1",
                    "name": "Mount Sinai Hospital",
                    "partOf": {"reference": "Organization/brand-msinai"},
                    "address": [
                        {
                            "text": "1 Gustave L. Levy Pl, New York, NY 10029",
                            "city": "New York",
                            "state": "NY",
                            "postalCode": "10029",
                        }
                    ],
                },
            },
            {
                "resource": {
                    "resourceType": "Organization",
                    "id": "loc-2",
                    "name": "Mount Sinai West",
                    "partOf": {"reference": "Organization/brand-msinai"},
                    "address": [{"city": "New York", "state": "NY", "postalCode": "10019"}],
                },
            },
        ],
    }


def test_imports_brand_and_locations(db):
    counts = import_brands_bundle(_bundle())

    brand = EhrBrand.objects.get(fhir_base_url="https://fhir.mountsinai.org/api/FHIR/R4")
    assert brand.name == "Mount Sinai"
    assert brand.npi_type_2 == "1234567890"
    assert brand.locations.count() == 2
    assert counts["brands"] == 1
    assert counts["locations"] == 2
    # A brand with no "sandbox" in its name is a real customer -> Epic Production, never the
    # sandbox vendor whose ehr_client_id only works against Epic's shared test FHIR server.
    assert brand.vendor.name == "Epic Production"


def test_sandbox_brand_gets_the_sandbox_vendor(db):
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "Endpoint",
                    "id": "ep-sandbox",
                    "address": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/",
                }
            },
            {
                "resource": {
                    "resourceType": "Organization",
                    "id": "brand-sandbox",
                    "name": "Epic Health System (Sandbox)",
                    "endpoint": [{"reference": "Endpoint/ep-sandbox"}],
                }
            },
        ],
    }
    import_brands_bundle(bundle)
    brand = EhrBrand.objects.get(fhir_base_url="https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/")
    assert brand.vendor.name == "Epic Sandbox"


def test_production_and_sandbox_brands_share_only_their_own_vendor(db):
    # Importing the curated sample (sandbox + AACI + Mount Sinai) must produce exactly the two
    # vendors, not one per brand and not one shared "Epic" vendor for everything.
    bundle = json.loads(Path(DEFAULT_SAMPLE).read_text(encoding="utf-8"))
    import_brands_bundle(bundle)

    assert set(EhrVendor.objects.values_list("name", flat=True)) == {"Epic Production", "Epic Sandbox"}
    sandbox = EhrVendor.objects.get(name="Epic Sandbox")
    production = EhrVendor.objects.get(name="Epic Production")
    assert list(sandbox.brands.values_list("name", flat=True)) == ["Epic Health System (Sandbox)"]
    assert set(production.brands.values_list("name", flat=True)) == {
        "Asian Americans for Community Involvement (AACI)",
        "Mount Sinai Health System",
    }

    # Re-importing must not create a second "Epic Production"/"Epic Sandbox" row.
    import_brands_bundle(bundle)
    assert EhrVendor.objects.count() == 2


def test_import_is_idempotent(db):
    import_brands_bundle(_bundle())
    import_brands_bundle(_bundle())
    assert EhrBrand.objects.count() == 1
    assert EhrBrandLocation.objects.count() == 2


def test_brand_without_facility_gets_fallback_location(db):
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "Endpoint",
                    "id": "ep9",
                    "address": "https://solo.example.org/FHIR/R4",
                }
            },
            {
                "resource": {
                    "resourceType": "Organization",
                    "id": "brand-solo",
                    "name": "Solo Clinic",
                    "endpoint": [{"reference": "Endpoint/ep9"}],
                    "address": [{"text": "500 Main St", "state": "CA"}],
                }
            },
        ],
    }
    import_brands_bundle(bundle)
    brand = EhrBrand.objects.get(fhir_base_url="https://solo.example.org/FHIR/R4")
    assert brand.locations.count() == 1
    assert brand.locations.first().name == "Solo Clinic"


def test_call_command_reads_file(db, tmp_path):
    path = tmp_path / "brands.json"
    path.write_text(json.dumps(_bundle()))
    call_command("import_ehr_brands", "--file", str(path))
    assert EhrBrand.objects.count() == 1
    assert EhrBrandLocation.objects.count() == 2


def test_imports_facility_with_urn_uuid_partof_reference(db):
    """Epic's real production Brands Bundle uses urn:uuid: references (not
    Organization/<id>) for both endpoint and partOf links."""
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {
                "fullUrl": "urn:uuid:ep-1",
                "resource": {
                    "resourceType": "Endpoint",
                    "id": "ep-1",
                    "address": "https://fhir.example.org/api/FHIR/R4",
                },
            },
            {
                "fullUrl": "urn:uuid:brand-1",
                "resource": {
                    "resourceType": "Organization",
                    "id": "brand-1",
                    "name": "Example Health",
                    "endpoint": [{"reference": "urn:uuid:ep-1"}],
                },
            },
            {
                "fullUrl": "urn:uuid:loc-1",
                "resource": {
                    "resourceType": "Organization",
                    "id": "loc-1",
                    "name": "Example Health Downtown Campus",
                    "partOf": {"reference": "urn:uuid:brand-1"},
                    "address": [{"text": "1 Example Way", "city": "Springfield", "state": "IL"}],
                },
            },
        ],
    }
    counts = import_brands_bundle(bundle)
    brand = EhrBrand.objects.get(fhir_base_url="https://fhir.example.org/api/FHIR/R4")
    assert counts["locations"] == 1
    assert brand.locations.get().name == "Example Health Downtown Campus"


def test_shipped_sample_fixture_is_valid_and_imports(db):
    bundle = json.loads(Path(DEFAULT_SAMPLE).read_text(encoding="utf-8"))
    counts = import_brands_bundle(bundle)
    assert counts["brands"] >= 1
    assert counts["locations"] >= 1
    # every seeded brand is selectable (has at least one location for the picker)
    assert all(b.locations.exists() for b in EhrBrand.objects.all())
