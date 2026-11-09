"""SMART scope parsing and coverage, including what the Epic sandbox actually grants."""

import pytest

from patient_access.core.models import OutcomeStatus
from patient_access.core.profiles import EPIC_PROFILE
from patient_access.core.puller import plan_outcomes
from patient_access.core.scopes import Constraint, Context, Permission, ResourceScope, ScopeSet
from patient_access.core.source_settings import ClientRegistration, SourceSettings

R, S = Permission.READ, Permission.SEARCH
LAB = "http://terminology.hl7.org/CodeSystem/observation-category|laboratory"
VITALS = "http://terminology.hl7.org/CodeSystem/observation-category|vital-signs"

# The scope parameter of the Epic sandbox's token response (2026-11-04), resource scopes split
# into read and search, some granular, every type the app registration lists.
EPIC_GRANTED = tuple(
    """patient/AllergyIntolerance.r patient/AllergyIntolerance.s patient/Binary.r patient/Binary.s
    patient/CarePlan.r patient/CarePlan.s patient/CareTeam.r patient/CareTeam.s patient/Condition.r
    patient/Condition.r?category=http://hl7.org/fhir/us/core/CodeSystem/condition-category|health-concern
    patient/Condition.r?category=http://terminology.hl7.org/CodeSystem/condition-category|encounter-diagnosis
    patient/Condition.r?category=http://terminology.hl7.org/CodeSystem/condition-category|problem-list-item
    patient/Condition.s patient/Coverage.r patient/Coverage.s patient/Device.r patient/Device.s
    patient/DiagnosticReport.r patient/DiagnosticReport.s patient/DocumentReference.r
    patient/DocumentReference.r?category=http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category|clinical-note
    patient/DocumentReference.s
    patient/DocumentReference.s?category=http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category|clinical-note
    patient/Encounter.r patient/Encounter.s patient/Goal.r patient/Goal.s patient/Immunization.r
    patient/Immunization.s patient/Medication.r patient/Medication.s patient/MedicationDispense.r
    patient/MedicationDispense.s patient/MedicationRequest.r patient/MedicationRequest.s
    patient/Observation.r patient/Observation.s patient/Patient.r patient/Patient.s patient/Procedure.r
    patient/Procedure.s patient/Provenance.r patient/QuestionnaireResponse.r patient/QuestionnaireResponse.s
    patient/ServiceRequest.r patient/ServiceRequest.s launch/patient openid profile""".split()
)


def epic_settings() -> SourceSettings:
    return SourceSettings(registration=ClientRegistration(vendor="epic", client_id="x"), fetch=EPIC_PROFILE)


@pytest.mark.parametrize(
    ("raw", "permissions"),
    [
        ("patient/Observation.read", {R, S}),
        ("patient/Observation.rs", {R, S}),
        ("patient/Observation.r", {R}),
        ("patient/Observation.s", {S}),
        ("patient/Observation.cruds", set(Permission)),
        ("patient/Observation.*", set(Permission)),
        ("patient/Observation.write", {Permission.CREATE, Permission.UPDATE, Permission.DELETE}),
    ],
)
def test_v1_and_v2_spellings_parse_to_permission_sets(raw, permissions):
    scope = ResourceScope.parse(raw)
    assert scope is not None
    assert (scope.context, scope.resource_type, scope.permissions) == (Context.PATIENT, "Observation", permissions)


@pytest.mark.parametrize(
    "raw",
    [
        "openid",
        "profile",
        "fhirUser",
        "launch/patient",
        "offline_access",
        "patient/Observation.sr",
        "patient/Observation.rr",
        "patient/Observation.x",
        "patient/Observation.",
        "clinic/Observation.read",
    ],
)
def test_anything_else_is_not_a_resource_scope(raw):
    assert ResourceScope.parse(raw) is None


def test_a_granular_scope_keeps_its_constraint():
    scope = ResourceScope.parse(f"patient/Observation.s?category={LAB}")
    assert scope is not None
    assert scope.constraints == {Constraint(name="category", value=LAB)}


def test_a_constraint_matches_on_code_and_on_system_when_both_name_one():
    granted = Constraint(name="category", value=LAB)
    assert granted.satisfied_by(Constraint(name="category", value="laboratory"))
    assert granted.satisfied_by(Constraint(name="category", value=LAB))
    assert not granted.satisfied_by(Constraint(name="category", value="vital-signs"))
    assert not granted.satisfied_by(Constraint(name="category", value="http://other.example/cs|laboratory"))
    assert not granted.satisfied_by(Constraint(name="code", value="laboratory"))


def test_epic_split_grants_cover_every_row_of_the_epic_profile():
    """The 2026-11-04 Epic sandbox run: every row came out not_granted under exact matching."""
    assert {q.label for q in epic_settings().queries_for(EPIC_GRANTED)} == {q.label for q in EPIC_PROFILE.queries}


def test_epic_split_grants_leave_nothing_requested_withheld():
    requested = epic_settings().scopes()
    assert ScopeSet.parse(EPIC_GRANTED).withheld(requested) == ()


def test_a_search_needs_s_and_the_patient_read_needs_r():
    settings = epic_settings()
    reads_only = tuple(s for s in EPIC_GRANTED if not s.endswith(".s") and ".s?" not in s)
    assert {q.label for q in settings.queries_for(reads_only)} == {"Demographics"}
    searches_only = tuple(s for s in EPIC_GRANTED if not s.endswith(".r") and ".r?" not in s)
    assert "Demographics" not in {q.label for q in settings.queries_for(searches_only)}
    assert "Labs" in {q.label for q in settings.queries_for(searches_only)}


def test_a_granular_grant_covers_only_its_own_category():
    settings = epic_settings()
    granted = (f"patient/Observation.s?category={LAB}",)
    labels = {q.label for q in settings.queries_for(granted)}
    assert "Labs" in labels
    assert "Vital Signs" not in labels
    assert ScopeSet.parse(granted).withheld(("patient/Observation.read",)) == ("patient/Observation.read",)


def test_a_wildcard_type_covers_every_row():
    settings = epic_settings()
    for wildcard in ("patient/*.read", "patient/*.rs", "patient/*.*"):
        assert len(settings.queries_for((wildcard,))) == len(EPIC_PROFILE.queries)


def test_withheld_names_what_the_ehr_left_out_as_requested():
    granted = tuple(s for s in EPIC_GRANTED if not s.startswith("patient/AllergyIntolerance."))
    requested = ("openid", "patient/AllergyIntolerance.read", f"patient/Observation.read?category={VITALS}")
    assert ScopeSet.parse(granted).withheld(requested) == ("patient/AllergyIntolerance.read",)


def test_outcomes_under_epic_grants_split_unticked_from_withheld():
    settings = epic_settings()
    requested = settings.scopes([q.scope for q in EPIC_PROFILE.queries if q.label not in {"Allergies"}])
    granted = tuple(s for s in EPIC_GRANTED if not s.startswith("patient/Immunization."))
    outcomes = {o.label: o.status for o in plan_outcomes(settings, requested, granted)}
    assert outcomes["Allergies"] is OutcomeStatus.NOT_REQUESTED  # unticked on our side
    assert outcomes["Immunizations"] is OutcomeStatus.NOT_GRANTED  # withheld at the EHR
    assert outcomes["Labs"] is OutcomeStatus.PENDING
    assert outcomes["Demographics"] is OutcomeStatus.PENDING


def test_a_user_grant_covers_a_patient_access():
    """In a patient's own launch the user is the patient; every query is pinned to their id."""
    settings = epic_settings()
    assert len(settings.queries_for(("user/*.read",))) == len(EPIC_PROFILE.queries)
    assert ScopeSet.parse(("user/Observation.rs",)).withheld(("patient/Observation.read",)) == ()
    assert ScopeSet.parse(("patient/Observation.rs",)).withheld(("user/Observation.read",)) == (
        "user/Observation.read",
    )  # but not the other way round
