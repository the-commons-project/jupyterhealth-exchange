"""Consent for a pull: a patient may launch, and a job may deliver, only while the patient has
consented a study to this data source. Read at launch, at the callback and again just before
delivery (the delivery guard), so a withdrawal between callback and delivery writes nothing."""

from __future__ import annotations

from typing import Any

from core.models import DataSource, DataSourceSupportedScope, Patient, StudyPatientScopeConsent
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


def require_consent(job: Job[Any]) -> None:
    """The library's delivery guard."""
    attrs = job.participant.attrs
    patient = Patient.objects.filter(id=getattr(attrs, "patient_id", None)).first()
    data_source = DataSource.objects.filter(id=getattr(attrs, "data_source_id", None)).first()
    if patient is None or data_source is None or not consenting_study_ids(patient, data_source):
        raise DeliveryRefused("You are no longer sharing your records with this study, so nothing was delivered.")
