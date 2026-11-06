"""Consent for a pull: a patient may launch, and a job may deliver, only while the patient has
consented a study to this data source. Read at launch, at the callback and again just before
delivery (the delivery guard), so a withdrawal between callback and delivery writes nothing."""

from __future__ import annotations

from core.models import DataSource, DataSourceSupportedScope, Patient, StudyPatientScopeConsent
from core.patient_access.identity import JheAttrs
from patient_access.core.models import Job
from patient_access.core.pipeline import DeliveryRefused


def consenting_study_ids(patient: Patient, data_source: DataSource) -> tuple[int, ...]:
    """Studies that use this data source and to which the patient consented one of its scopes."""
    scopes = DataSourceSupportedScope.objects.filter(data_source=data_source).values("scope_code")
    ids = (
        StudyPatientScopeConsent.objects.filter(
            consented=True,
            study_patient__patient=patient,
            study_patient__study__studydatasource__data_source=data_source,
            scope_code__in=scopes,
        )
        .values_list("study_patient__study_id", flat=True)
        .distinct()
    )
    return tuple(sorted(ids))


def require_consent(job: Job[JheAttrs]) -> JheAttrs:
    """The library's delivery guard. Returns the attributes with the studies the patient consents
    to now, so the bundle names those rather than the list fixed when the pull started."""
    attrs = job.participant.attrs
    patient = Patient.objects.filter(id=attrs.patient_id).first()
    data_source = DataSource.objects.filter(id=attrs.data_source_id).first()
    study_ids = consenting_study_ids(patient, data_source) if patient and data_source else ()
    if not study_ids:
        raise DeliveryRefused("You are no longer sharing your records with this study, so nothing was delivered.")
    return attrs.model_copy(update={"study_ids": study_ids})
