import uuid

from django.db import models
from django.shortcuts import get_object_or_404

from core.fhir.config import aux_resource_types

from .practitioner import Practitioner


class FhirAuxResource(models.Model):
    """An *auxiliary* FHIR resource stored as an opaque JSON blob.

    Every FHIR resource that does not fit the JHE-system view of a mapped Django model is
    stored here: its entire FHIR body lives in ``fhir_data`` and the server performs no
    computation on it beyond plain CRUD. The supported resource types are declared under
    ``aux_resources`` in core/fhir/fhir_config.json (mapped types appear there too -- their
    non-system rows fall through to this store).

    The primary key (and therefore the FHIR-facing ``id``) is a UUID, keeping it disjoint
    from the integer pks of the mapped models so a request can be routed by id shape. Every row
    is linked to a ``fhir_source`` (required) -- the patient-registered upstream source it was
    uploaded through, identified by the ``X-JHE-FHIR-Source-ID`` header -- and to that source's
    ``patient``. ``fhir_resource_id`` and ``patient_fhir_id`` are best-effort copies of the
    resource's own ``id`` and its referenced Patient id (both may be null).
    """

    # Not passed to the field — keeps choices out of migration state so adding/removing
    # resource types in fhir_config.json never requires a migration.
    RESOURCE_TYPE_CHOICES = [(name, name) for name in sorted(aux_resource_types())]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey("Patient", on_delete=models.CASCADE)
    fhir_source = models.ForeignKey("FhirSource", on_delete=models.CASCADE, related_name="aux_resources")
    resource_type = models.CharField()
    patient_fhir_id = models.CharField(null=True, blank=True)
    fhir_resource_id = models.CharField(null=True, blank=True)
    fhir_data = models.JSONField(null=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["resource_type", "patient"]),
        ]

    def __str__(self):
        return f"{self.resource_type}/{self.pk}"

    @staticmethod
    def for_patient(patient, resource_type):
        # Auxiliary resources for a single patient (the resolved FhirSource patient).
        return FhirAuxResource.objects.filter(resource_type=resource_type, patient=patient).order_by("-last_updated")

    @staticmethod
    def fhir_search(jhe_user_id, resource_type):
        # The auxiliary resources a practitioner may see when no source is named: those for any
        # patient who shares an organization with the practitioner. Used by header-less reads.
        practitioner = get_object_or_404(Practitioner, jhe_user_id=jhe_user_id)
        return (
            FhirAuxResource.objects.filter(
                resource_type=resource_type,
                patient__organizations__practitioners=practitioner,
            )
            .distinct()
            .order_by("-last_updated")
        )
