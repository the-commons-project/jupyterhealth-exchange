from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from core.fhir.ref_indexing import index_fhir_source_refs
from core.models import EhrBrand, FhirSource
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
        return FhirSource.objects.filter(patient=patient).select_related("ehr_brand_location").order_by("-last_updated")

    def create(self, request, *args, **kwargs):
        """Register a source, or return the patient's existing one for the same EHR brand.

        A patient has one source per EHR brand and data source, so reconnecting does not stack a second copy of every record under a fresh source -- aux rows are only unique within a source. The brand comes from the picked facility, or from ``ehr_base_url`` (the SMART ``iss``) when none was picked. A new EHR source with no label is labelled "<vendor name> - <brand name>". A source that resolves to no brand is not an EHR connection and always creates.
        """
        patient = request.user.get_patient()
        if patient is None:
            raise PermissionDenied("Only patient users can register a FhirSource.")
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Not a model field: consumed here, so it must not reach serializer.save().
        base_url = serializer.validated_data.pop("ehr_base_url", "")

        brand = self._resolve_brand(serializer.validated_data.get("ehr_brand_location"), base_url)
        if brand is not None and not serializer.validated_data.get("label"):
            # Patient-facing, so it names the vendor and brand rather than anything technical.
            serializer.validated_data["label"] = " - ".join(filter(None, [brand.vendor.name, brand.name]))
        if brand is not None:
            existing = (
                FhirSource.objects.filter(
                    patient=patient,
                    data_source=serializer.validated_data["data_source"],
                    ehr_brand_location__brand=brand,
                )
                .order_by("id")
                .first()
            )
            if existing is not None:
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)

        self.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _resolve_brand(location, base_url):
        """The EhrBrand a registration is for: the picked facility's brand, else the one served at ``base_url``.

        ``base_url`` is matched with and without a trailing slash, since ``iss`` comes from the launch rather than the brands table and the two may differ only in that.
        """
        if location is not None:
            return location.brand
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url:
            return None
        return (
            EhrBrand.objects.filter(Q(fhir_base_url=base_url) | Q(fhir_base_url=base_url + "/")).order_by("id").first()
        )

    def perform_create(self, serializer):
        patient = self.request.user.get_patient()
        if patient is None:
            raise PermissionDenied("Only patient users can register a FhirSource.")
        serializer.save(patient=patient)

    @action(detail=True, methods=["POST"])
    def index_refs(self, request, pk=None):
        """Rewrite this source's aux-resource references from upstream ids to JHE ids (#584).

        get_object() enforces ownership (the queryset is the caller's own sources). Returns a
        summary of rows indexed and references rewritten / not found.
        """
        fhir_source = self.get_object()
        return Response(index_fhir_source_refs(fhir_source))
