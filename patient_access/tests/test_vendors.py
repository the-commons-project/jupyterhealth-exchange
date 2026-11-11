"""The vendor registry: what is the same in every deployment."""

from patient_access.core.vendors import VENDORS, TypeChoice, VendorKey


def test_epic_asks_per_type_on_its_own_screen_and_the_reference_server_does_not():
    assert VENDORS[VendorKey.EPIC].type_choice is TypeChoice.EHR
    assert VENDORS[VendorKey.SMART_REFERENCE].type_choice is TypeChoice.HOST


def test_a_vendor_without_an_entry_gets_the_standard_table_and_our_checklist():
    baseline = VENDORS[VendorKey.US_CORE]
    assert baseline.fetch.name == "uscdi" and baseline.type_choice is TypeChoice.HOST
    assert baseline.base_scopes == ("openid", "fhirUser", "launch/patient")


def test_every_vendor_settings_carries_its_registration_and_profile():
    for key, vendor in VENDORS.items():
        settings = vendor.settings(vendor_name=f"{key} Sandbox", client_id="c")
        assert settings.registration.client_id == "c"
        assert settings.fetch is vendor.fetch
