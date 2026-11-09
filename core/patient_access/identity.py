"""Who a pull is for, in JHE's terms: the patient, the data source and, once the EHR answered,
the FhirSource the pull is recorded under."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from core.models import DataSource, FhirSource, Patient
from core.models.fhir_aux_resource import fhir_source_uri
from patient_access.core.models import Participant

PROVIDER = "jhe_patient_session"


class LaunchAttrs(BaseModel):
    """On the auth attempt: the FhirSource does not exist yet at launch. `callback_cookie_sha256`
    is the SHA-256 of the cookie the launch set in the patient's browser; the callback acts only
    for a browser that presents it. The cookie's value is never stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    patient_id: int
    data_source_id: int
    callback_cookie_sha256: str


class JheAttrs(BaseModel):
    """On the job: what the key layout and the bundle's provenance read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    patient_id: int
    data_source_id: int
    fhir_source_id: int
    fhir_source_uri: str  # becomes Composition.meta.source
    study_ids: tuple[int, ...] = ()  # the studies the patient consented this data source to


def launch_participant(patient: Patient, data_source_id: int, callback_cookie_sha256: str) -> Participant[LaunchAttrs]:
    return Participant[LaunchAttrs](
        ref=str(patient.id),
        provider=PROVIDER,
        attrs=LaunchAttrs(
            patient_id=patient.id, data_source_id=data_source_id, callback_cookie_sha256=callback_cookie_sha256
        ),
    )


def pull_participant(
    patient: Patient, data_source: DataSource, fhir_source: FhirSource, study_ids: tuple[int, ...]
) -> Participant[JheAttrs]:
    return Participant[JheAttrs](
        ref=str(patient.id),
        provider=PROVIDER,
        attrs=JheAttrs(
            patient_id=patient.id,
            data_source_id=data_source.id,
            fhir_source_id=fhir_source.id,
            fhir_source_uri=fhir_source_uri(fhir_source.id),
            study_ids=study_ids,
        ),
    )
