"""Building one vendor's settings from what the host's row says."""

import pytest

from patient_access.core.profiles import USCDI_PROFILE
from patient_access.core.source_settings import BASE_SCOPES
from patient_access.core.vendors import check_query_override, overridable_rows, vendor_settings
from patient_access.core.vocab import ScopeVersion, TypeChoice


def query_of(settings, label):
    return next(q.query for q in settings.fetch.queries if q.label == label)


def test_by_default_a_vendor_gets_the_uscdi_table_v1_scopes_the_base_scopes_and_our_checklist():
    settings = vendor_settings(vendor_name="Some EHR", client_id="c")
    assert settings.fetch == USCDI_PROFILE
    assert settings.registration.client_id == "c" and settings.registration.base_scopes == BASE_SCOPES
    assert settings.registration.scope_version is ScopeVersion.V1 and settings.type_choice is TypeChoice.HOST


def test_the_host_adds_scopes_to_the_base_set_and_sets_the_type_choice():
    settings = vendor_settings(
        vendor_name="Epic Sandbox", client_id="c", extra_scopes=("profile", "openid"), type_choice=TypeChoice.EHR
    )
    assert settings.registration.base_scopes == (*BASE_SCOPES, "profile")  # never fewer, never twice
    assert settings.type_choice is TypeChoice.EHR


def test_a_v2_vendor_requests_v2_scopes():
    settings = vendor_settings(vendor_name="v2 EHR", client_id="c", scope_version=ScopeVersion.V2)
    assert settings.registration.scopes(["patient/Observation.read"])[-1] == "patient/Observation.rs"


def test_an_override_replaces_only_its_row_and_keeps_the_rest_of_it():
    settings = vendor_settings(
        vendor_name="Sandbox",
        client_id="c",
        query_overrides={"Documents": "DocumentReference", "Summary Documents": "DocumentReference/$docref"},
    )
    assert query_of(settings, "Documents") == "DocumentReference"
    summary = next(q for q in settings.fetch.queries if q.label == "Summary Documents")
    assert summary.query == "DocumentReference/$docref" and summary.alternatives  # fallbacks kept
    assert query_of(settings, "Labs") == query_of(vendor_settings(vendor_name="x", client_id="c"), "Labs")
    assert settings.fetch.name == USCDI_PROFILE.name


def test_the_patient_instance_read_is_not_overridable():
    labels = {q.label for q in overridable_rows()}
    assert "Demographics" not in labels and {"Documents", "Labs", "Summary Documents"} <= labels


@pytest.mark.parametrize(
    "query",
    [
        "DocumentReference",
        "DocumentReference?category=clinical-note&_count=50",
        "DocumentReference/$docref",
    ],
)
def test_a_search_on_the_rows_own_resource_type_is_accepted(query):
    check_query_override("Documents", query)


@pytest.mark.parametrize(
    ("label", "query", "message"),
    [
        ("No Such Row", "DocumentReference", "Unknown query row"),
        ("Demographics", "Patient", "Unknown query row"),  # instance read
        ("Documents", "", "non-empty"),
        ("Documents", "DocumentReference ", "no spaces"),
        ("Documents", "DocumentReference?category=a b", "no spaces"),
        ("Documents", "Observation", "must start with DocumentReference"),  # escapes the row's scope
        ("Documents", "DocumentReferenceX", "must start with DocumentReference"),
        ("Documents", "https://evil.example/DocumentReference", "relative to the FHIR base"),
        ("Documents", "/DocumentReference", "relative to the FHIR base"),
        ("Documents", "DocumentReference/../Observation", "relative to the FHIR base"),
        ("Documents", "DocumentReference#x", "relative to the FHIR base"),
    ],
)
def test_a_search_that_could_leave_its_row_is_refused(label, query, message):
    with pytest.raises(ValueError, match=message):
        check_query_override(label, query)


def test_the_builder_refuses_a_bad_override_that_bypassed_the_host_check():
    with pytest.raises(ValueError):
        vendor_settings(vendor_name="x", client_id="c", query_overrides={"Documents": "Observation"})
