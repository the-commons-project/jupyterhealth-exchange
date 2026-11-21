"""Tests for import_sandbox_ehrs (vendor sandbox rows and brands from core/data/ehr_sandboxes.json)."""

import io
import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.management.commands.import_sandbox_ehrs import DEFAULT_FILE
from core.models import EhrBrand, EhrBrandLocation, EhrVendor

ENTRIES = json.loads(Path(DEFAULT_FILE).read_text())["sandboxes"]
NAMES = {e["vendor"] for e in ENTRIES}


def _run(*args):
    out = io.StringIO()
    call_command("import_sandbox_ehrs", *args, stdout=out)
    return out.getvalue()


def test_file_lists_every_vendor_once_with_a_unique_url():
    assert NAMES == {
        "Epic Sandbox",
        "Oracle Health Sandbox",
        "eClinicalWorks Sandbox",
        "athenahealth Sandbox",
        "Altera Sandbox",
    }
    assert len({e["fhir_base_url"] for e in ENTRIES}) == len(ENTRIES)
    assert not any("client_id" in e for e in ENTRIES)  # client ids are per deployment, never committed


@pytest.mark.django_db
def test_creates_rows_brands_and_locations_with_vendor_defaults():
    _run()
    assert set(EhrVendor.objects.values_list("name", flat=True)) >= NAMES
    for e in ENTRIES:
        brand = EhrBrand.objects.get(fhir_base_url=e["fhir_base_url"])
        assert brand.vendor.name == e["vendor"]
        assert brand.locations.count() == 1
    assert EhrVendor.objects.get(name="Epic Sandbox").skip_type_checklist is True
    assert EhrVendor.objects.get(name="Oracle Health Sandbox").extra_scopes == "patient/Binary.read"
    assert EhrVendor.objects.get(name="athenahealth Sandbox").extra_scopes == ""


@pytest.mark.django_db
def test_idempotent_and_client_id_only_when_given():
    _run("--client-id", "athenahealth Sandbox=0oaTEST")
    _run()  # a rerun without --client-id must not clear it
    _run()
    assert EhrVendor.objects.get(name="athenahealth Sandbox").ehr_client_id == "0oaTEST"
    assert EhrVendor.objects.get(name="Epic Sandbox").ehr_client_id in (None, "")
    assert EhrBrand.objects.filter(vendor__name__in=NAMES).count() == len(ENTRIES)
    assert EhrBrandLocation.objects.filter(brand__vendor__name__in=NAMES).count() == len(ENTRIES)


@pytest.mark.django_db
def test_existing_brand_is_moved_and_not_given_a_second_location():
    other = EhrVendor.objects.create(name="Epic Production")
    brand = EhrBrand.objects.create(
        name="Epic Sandbox (from the sample)",
        vendor=other,
        fhir_base_url="https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",  # no trailing slash
    )
    EhrBrandLocation.objects.create(brand=brand, name="Epic Sandbox (from the sample)")
    out = _run("--only", "Epic Sandbox")
    brand.refresh_from_db()
    assert brand.vendor.name == "Epic Sandbox"
    assert brand.name == "Epic Sandbox (from the sample)"  # the existing name wins
    assert brand.locations.count() == 1
    assert EhrBrand.objects.count() == 1
    assert "moved" in out


@pytest.mark.django_db
def test_unknown_row_is_an_error():
    with pytest.raises(CommandError):
        _run("--client-id", "Nope=abc")
    with pytest.raises(CommandError):
        _run("--client-id", "no-equals-sign")
