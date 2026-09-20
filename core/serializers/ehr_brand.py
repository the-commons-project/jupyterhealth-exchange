from rest_framework import serializers

from core.models import EhrBrand, EhrBrandLocation, EhrVendor


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


class EhrVendorSerializer(serializers.ModelSerializer):
    # name is nullable on the model but required when creating a vendor via the API
    name = serializers.CharField()
    brands = EhrBrandSerializer(many=True, read_only=True)

    class Meta:
        model = EhrVendor
        fields = ["id", "name", "ehr_client_id", "supported_scopes", "brands", "last_updated"]
        read_only_fields = ["id", "brands", "last_updated"]
