from rest_framework.exceptions import PermissionDenied
from rest_framework.viewsets import ModelViewSet

from core.models import FhirSource
from core.serializers import FhirSourceSerializer


class FhirSourceViewSet(ModelViewSet):
    """Simple CRUD for a patient to register and manage their own FhirSources.

    Scoped to the requesting patient user: a patient only ever sees and edits their own
    sources, and ``patient`` is assigned from the authenticated user on create.
    """

    serializer_class = FhirSourceSerializer
    model_class = FhirSource

    def get_queryset(self):
        patient = self.request.user.get_patient()
        if patient is None:
            return FhirSource.objects.none()
        return FhirSource.objects.filter(patient=patient).order_by("-last_updated")

    def perform_create(self, serializer):
        patient = self.request.user.get_patient()
        if patient is None:
            raise PermissionDenied("Only patient users can register a FhirSource.")
        serializer.save(patient=patient)
