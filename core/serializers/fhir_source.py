from django.db.models import Count
from rest_framework import serializers

from core.models import FhirSource


class FhirSourceSerializer(serializers.ModelSerializer):
    """A patient-registered upstream FHIR source; ``patient`` is set server-side from the requesting user, never the request body."""

    resource_counts = serializers.SerializerMethodField()
    ehr_brand_location_name = serializers.SerializerMethodField()
    # A lookup hint for FhirSourceViewSet.create, never stored: the SMART ``iss`` the patient authorized against, used to find the brand when no facility was picked.
    ehr_base_url = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = FhirSource
        fields = [
            "id",
            "patient",
            "data_source",
            "label",
            "ehr_brand_location",
            "ehr_brand_location_name",
            "ehr_base_url",
            "resource_counts",
            "last_updated",
        ]
        read_only_fields = ["id", "patient", "last_updated"]

    def get_resource_counts(self, obj):
        """{resource_type: rows stored under this source}, the per-type receipt the patient pages show."""
        counts = (
            obj.aux_resources.values("resource_type")
            .annotate(n=Count("id"))
            .order_by()
            .values_list("resource_type", "n")
        )
        return dict(counts)

    def get_ehr_brand_location_name(self, obj):
        return obj.ehr_brand_location.name if obj.ehr_brand_location else None


class PatientFhirSourceSerializer(serializers.ModelSerializer):
    """A patient's FhirSource as the admin patient page lists it: which data source, its label and when it last changed."""

    data_source_name = serializers.CharField(source="data_source.name", read_only=True)

    class Meta:
        model = FhirSource
        fields = ["id", "data_source", "data_source_name", "label", "last_updated"]
        read_only_fields = fields
