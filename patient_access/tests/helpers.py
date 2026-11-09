"""Shared values for the patient_access library tests: a host's attrs, a source, a job, settings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, SecretStr

from patient_access.core.models import (
    AuthAttempt,
    Brand,
    Credentials,
    Endpoint,
    Facility,
    Job,
    Participant,
    Selection,
    Source,
    TokenResponse,
)
from patient_access.core.puller import plan_outcomes
from patient_access.core.settings import PatientAccessSettings, S3Settings
from patient_access.core.source_settings import SourceSettings
from patient_access.core.vendors import VENDORS, VendorKey
from patient_access.core.vocab import AuthMethodName, ScopeVersion

ISS = "https://ehr.example.org/fhir/r4"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class HostAttrs(BaseModel):
    patient_id: int
    data_source_id: int
    fhir_source_id: int
    fhir_source_uri: str = "https://jhe.example.org/fhir-source/7"


class LaunchAttrs(BaseModel):
    patient_id: int
    data_source_id: int


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


ENDPOINT = Endpoint(id="b1", iss=ISS, vendor="Example Vendor")
SELECTION = Selection(
    id="loc1",
    brand=Brand(id="b1", name="Example Health", endpoint=ENDPOINT, npi="1234567890"),
    facility=Facility(id="loc1", name="Example Clinic", address="1 Main St", city="Boston", state="MA"),
)


def source_settings(client_id: str = "client-1") -> SourceSettings:
    return VENDORS[VendorKey.SMART_REFERENCE].settings(vendor_name="Example Vendor", client_id=client_id)


def participant(**attrs: int) -> Participant[HostAttrs]:
    values = {"patient_id": 40001, "data_source_id": 3, "fhir_source_id": 7} | attrs
    return Participant[HostAttrs](ref=str(values["patient_id"]), provider="test", attrs=HostAttrs(**values))


def launch_participant() -> Participant[LaunchAttrs]:
    return Participant[LaunchAttrs](ref="40001", provider="test", attrs=LaunchAttrs(patient_id=40001, data_source_id=3))


def attempt(**overrides: object) -> AuthAttempt[LaunchAttrs]:
    values: dict[str, object] = {
        "state": "state-1",
        "code_verifier": SecretStr("verifier"),
        "participant": launch_participant(),
        "selection": SELECTION,
        "scopes": ("openid", "launch/patient", "patient/Observation.read", "patient/Patient.read"),
        "scopes_declined": ("patient/Condition.read",),
        "redirect_uri": "https://jhe.example.org/callback",
        "token_endpoint": "https://ehr.example.org/token",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "auth_method": AuthMethodName.PUBLIC,
        "scope_version": ScopeVersion.V1,
    } | overrides
    return AuthAttempt[LaunchAttrs].model_validate(values)


def token(**overrides: object) -> TokenResponse:
    values: dict[str, object] = {
        "access_token": SecretStr("ehr-token"),
        "expires_at": NOW + timedelta(hours=1),
        "scopes_granted": ("openid", "launch/patient", "patient/Observation.read", "patient/Patient.read"),
        "patient": "p1",
    } | overrides
    return TokenResponse.model_validate(values)


def job(job_id: str = "job-1", **overrides: object) -> Job[HostAttrs]:
    a, t = attempt(), token()
    values: dict[str, object] = {
        "id": job_id,
        "participant": participant(),
        "source": Source.of(a, t),
        "credentials": Credentials(ehr_access_token=t.access_token),
        "token_expires_at": t.expires_at,
        "outcomes": plan_outcomes(source_settings(), a.scopes, t.scopes_granted),
        "created_at": NOW,
    } | overrides
    return Job[HostAttrs].model_validate(values)


def settings(**s3: object) -> PatientAccessSettings:
    return PatientAccessSettings(s3=S3Settings.model_validate({"bucket": "pulls"} | s3))


def bundle_of(**resource: object) -> dict[str, object]:
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": resource}]}


# --- host plugs named by dotted path in PATIENT_ACCESS (module "patient_access.tests.helpers") ---


class HostDirectory:
    """A directory constructed with no arguments, as the module's config requires."""

    def selection(self, selection_id: str) -> Selection:
        if selection_id != SELECTION.id:
            raise KeyError(selection_id)
        return SELECTION

    def settings_for(self, endpoint: Endpoint) -> SourceSettings:
        return source_settings()


def host_settings() -> PatientAccessSettings:
    return settings()


def host_guard(job: Job[HostAttrs]) -> HostAttrs:
    return job.participant.attrs


HELPERS = "patient_access.tests.helpers"
TEST_KEY = "x9Q5mX1y8m0bkYb3l7wVvO9Oe2r3wQ4b8h9vB9fP0nU="


def module_config(**overrides: object) -> dict[str, object]:
    return {
        "ENCRYPTION_KEYS": [TEST_KEY],
        "SPOOL_DIR": "/tmp/patient-access-test-spool",
        "DISPATCHER": "none",
        "LAUNCH_ATTRS": f"{HELPERS}.LaunchAttrs",
        "JOB_ATTRS": f"{HELPERS}.HostAttrs",
        "DIRECTORY": f"{HELPERS}.HostDirectory",
        "SETTINGS_PROVIDER": f"{HELPERS}.host_settings",
        "DELIVERY_GUARD": f"{HELPERS}.host_guard",
    } | overrides
