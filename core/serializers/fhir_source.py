from rest_framework import serializers

from core.models import FhirSource


class FhirSourceSerializer(serializers.ModelSerializer):
    """A patient-registered upstream FHIR source. ``patient`` is set server-side from the
    requesting user, never the request body."""

    class Meta:
        model = FhirSource
        fields = ["id", "patient", "data_source", "label", "ehr_brand_location", "last_updated"]
        read_only_fields = ["id", "patient", "last_updated"]
