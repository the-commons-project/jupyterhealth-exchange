from django.db import models
from django.shortcuts import get_object_or_404

from core.fhir.config import aux_resource_types

from .practitioner import Practitioner


class FhirAuxResource(models.Model):
    """An *auxiliary* FHIR resource stored as an opaque JSON blob.

    Unlike the mapped resources (Patient, Observation) whose fields are projected
    onto dedicated Django columns, an auxiliary resource has no per-field mapping:
    its entire FHIR body lives in ``fhir_data`` and the server performs no
    computation on it beyond plain CRUD. The supported resource types are declared
    under ``aux_resources`` in core/fhir/fhir_config.json.

    The FHIR-facing ``id`` is always the Django primary key (set on read by the
    serializer); ``fhir_resource_id`` retains the resource's original/source id.
    """

    # Not passed to the field — keeps choices out of migration state so adding/removing
    # resource types in fhir_config.json never requires a migration.
    RESOURCE_TYPE_CHOICES = [(name, name) for name in sorted(aux_resource_types())]

    patient = models.ForeignKey("Patient", on_delete=models.CASCADE, null=True, blank=True)
    resource_type = models.CharField()
    patient_fhir_id = models.CharField(null=True, blank=True)
    fhir_resource_id = models.CharField()
    fhir_data = models.JSONField(null=True)
    source = models.CharField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["resource_type", "patient"]),
        ]

    def __str__(self):
        return f"{self.resource_type}/{self.pk}"

    @staticmethod
    def fhir_search(jhe_user_id, resource_type):
        # Auxiliary resources a practitioner may see: those attached to a patient who shares
        # an organization with the practitioner, plus org-agnostic resources with no patient.
        # Formatting into FHIR JSON is the serializer's job.
        practitioner = get_object_or_404(Practitioner, jhe_user_id=jhe_user_id)
        return (
            FhirAuxResource.objects.filter(resource_type=resource_type)
            .filter(models.Q(patient__isnull=True) | models.Q(patient__organizations__practitioners=practitioner))
            .distinct()
            .order_by("-last_updated")
        )

    @staticmethod
    def for_patient(patient, resource_type):
        # Auxiliary resources owned by a single patient (patient-user access path).
        return FhirAuxResource.objects.filter(resource_type=resource_type, patient=patient).order_by("-last_updated")
