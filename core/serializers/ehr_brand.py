from rest_framework import serializers

from core.models import EhrBrand, EhrBrandLocation, EhrVendor
from core.models.ehr_brand import query_override_errors


class EhrVendorSettingsMixin:
    """The checks EhrVendor.clean makes, which a ModelSerializer does not call."""

    def validate_query_overrides(self, value):
        if errors := query_override_errors(value):
            raise serializers.ValidationError(errors)
        return value


class EhrBrandLocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = EhrBrandLocation
        fields = ["id", "name", "address_text", "city", "state", "postal_code", "country"]


class EhrBrandSerializer(serializers.ModelSerializer):
    # Read-only: EhrBrand/EhrBrandLocation are managed by manage.py import_ehr_brands and the
    # plain Django admin, not the EHRs admin page -- only EhrVendor is editable there.
    locations = EhrBrandLocationSerializer(many=True, read_only=True)

    class Meta:
        model = EhrBrand
        fields = ["id", "name", "fhir_base_url", "fhir_version", "npi_type_2", "logo_url", "locations", "last_updated"]
        read_only_fields = fields


class EhrVendorListSerializer(EhrVendorSettingsMixin, serializers.ModelSerializer):
    # The vendor list (thousands of brands/locations nested under some vendors) is expensive to
    # serialize and unused by the list table, which only shows name/client id/scopes/sign-in settings -- brands
    # are fetched separately, per vendor, by EhrVendorSerializer on the detail view.
    name = serializers.CharField()

    class Meta:
        model = EhrVendor
        fields = [
            "id",
            "name",
            "ehr_client_id",
            "supported_scopes",
            "skip_type_checklist",
            "extra_scopes",
            "scope_version",
            "query_overrides",
            "last_updated",
        ]
        read_only_fields = ["id", "last_updated"]


class EhrVendorSerializer(EhrVendorSettingsMixin, serializers.ModelSerializer):
    # name is nullable on the model but required when creating a vendor via the API
    name = serializers.CharField()
    brands = EhrBrandSerializer(many=True, read_only=True)

    class Meta:
        model = EhrVendor
        fields = [
            "id",
            "name",
            "ehr_client_id",
            "supported_scopes",
            "skip_type_checklist",
            "extra_scopes",
            "scope_version",
            "query_overrides",
            "brands",
            "last_updated",
        ]
        read_only_fields = ["id", "brands", "last_updated"]
