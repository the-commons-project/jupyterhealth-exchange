"""Fetch profiles in code: the USCDI query table and the per-vendor variations we know about."""

from __future__ import annotations

from .source_settings import FetchProfile, QuerySpec

# USCDI for a patient-facing pull: one row per type (two for Observation, by category).
USCDI: tuple[QuerySpec, ...] = (
    QuerySpec(
        label="Demographics", resource_type="Patient", query="Patient", scope="patient/Patient.read", single=True
    ),
    QuerySpec(label="Conditions", resource_type="Condition", query="Condition", scope="patient/Condition.read"),
    QuerySpec(
        label="Medications",
        resource_type="MedicationRequest",
        query="MedicationRequest",
        scope="patient/MedicationRequest.read",
    ),
    QuerySpec(
        label="Medication Dispenses",
        resource_type="MedicationDispense",
        query="MedicationDispense",
        scope="patient/MedicationDispense.read",
    ),
    QuerySpec(
        label="Allergies",
        resource_type="AllergyIntolerance",
        query="AllergyIntolerance",
        scope="patient/AllergyIntolerance.read",
    ),
    QuerySpec(
        label="Immunizations", resource_type="Immunization", query="Immunization", scope="patient/Immunization.read"
    ),
    QuerySpec(label="Procedures", resource_type="Procedure", query="Procedure", scope="patient/Procedure.read"),
    QuerySpec(
        label="Labs",
        resource_type="Observation",
        query="Observation?category=laboratory",
        scope="patient/Observation.read",
    ),
    QuerySpec(
        label="Vital Signs",
        resource_type="Observation",
        query="Observation?category=vital-signs",
        scope="patient/Observation.read",
    ),
    QuerySpec(
        label="Diagnostic Reports",
        resource_type="DiagnosticReport",
        query="DiagnosticReport",
        scope="patient/DiagnosticReport.read",
    ),
    QuerySpec(
        label="Documents",
        resource_type="DocumentReference",
        query="DocumentReference?category=clinical-note",
        scope="patient/DocumentReference.read",
    ),
    QuerySpec(label="Encounters", resource_type="Encounter", query="Encounter", scope="patient/Encounter.read"),
    QuerySpec(
        label="Care Plans",
        resource_type="CarePlan",
        query="CarePlan?category=assess-plan",
        scope="patient/CarePlan.read",
    ),
    QuerySpec(
        label="Care Teams", resource_type="CareTeam", query="CareTeam?status=active", scope="patient/CareTeam.read"
    ),
    QuerySpec(label="Goals", resource_type="Goal", query="Goal", scope="patient/Goal.read"),
    QuerySpec(
        label="Service Requests",
        resource_type="ServiceRequest",
        query="ServiceRequest",
        scope="patient/ServiceRequest.read",
    ),
    QuerySpec(label="Devices", resource_type="Device", query="Device", scope="patient/Device.read"),
    QuerySpec(
        label="Questionnaire Responses",
        resource_type="QuestionnaireResponse",
        query="QuestionnaireResponse",
        scope="patient/QuestionnaireResponse.read",
    ),
)

USCDI_PROFILE = FetchProfile(name="uscdi", queries=USCDI)

# Epic: the USCDI table as published.
EPIC_PROFILE = USCDI_PROFILE.model_copy(update={"name": "epic"})

# The SMART reference server leaves DocumentReference.category empty; the clinical-note filter
# would hide every note there.
SMART_REFERENCE_PROFILE = USCDI_PROFILE.with_query("Documents", "DocumentReference", name="smart-reference")
