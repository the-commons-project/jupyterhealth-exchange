from rest_framework import serializers

from core.fhir.config import get_resource_mapping
from core.fhir.engine import build_fhir_resource
from core.models import JheUser, Patient, PatientIdentifier

from .organization import OrganizationSerializer


class PatientIdentifierSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientIdentifier
        fields = ["id", "system", "value"]


class PatientSerializer(serializers.ModelSerializer):
    telecom_email = serializers.SerializerMethodField()
    organizations = serializers.SerializerMethodField()
    identifiers = serializers.SerializerMethodField()

    def get_telecom_email(self, obj):
        if obj.telecom_email:
            return obj.telecom_email
        else:
            return obj.jhe_user.email

    def get_organizations(self, obj):
        organizations = obj.organizations.all()
        return OrganizationSerializer(organizations, many=True).data

    def get_identifiers(self, obj):
        return PatientIdentifierSerializer(obj.identifiers.all(), many=True).data

    class Meta:
        model = Patient
        fields = [
            "id",
            "jhe_user_id",
            "identifiers",
            "name_family",
            "name_given",
            "birth_date",
            "telecom_phone",
            "telecom_email",
            "organizations",
        ]


class PatientProfileSerializer(serializers.ModelSerializer):
    """Patient serializer with PHI stripped for patient-facing profile endpoint."""

    organizations = serializers.SerializerMethodField()
    identifiers = serializers.SerializerMethodField()

    def get_organizations(self, obj):
        organizations = obj.organizations.all()
        return OrganizationSerializer(organizations, many=True).data

    def get_identifiers(self, obj):
        return PatientIdentifierSerializer(obj.identifiers.all(), many=True).data

    class Meta:
        model = Patient
        fields = [
            "id",
            "jhe_user_id",
            "identifiers",
            "organizations",
        ]


class JheUserSerializer(serializers.ModelSerializer):
    patient = PatientSerializer(many=False, read_only=True)

    class Meta:
        model = JheUser
        fields = ["id", "email", "first_name", "last_name", "patient", "user_type", "is_superuser"]


class JheUserPatientProfileSerializer(serializers.ModelSerializer):
    """User serializer with PHI stripped for patient users on the profile endpoint."""

    patient = PatientProfileSerializer(many=False, read_only=True)

    class Meta:
        model = JheUser
        fields = ["id", "patient", "user_type", "is_superuser"]


class FHIRPatientSerializer(serializers.Serializer):
    """Renders a Patient model instance into a FHIR R5 Patient resource.

    The shape is driven entirely by the mapping in core/fhir/fhir_config.json. Output is not
    validated against fhir.resources -- the rendered fields come from validated columns.
    """

    def to_representation(self, patient):
        mapping = get_resource_mapping("Patient")
        return build_fhir_resource(patient, "Patient", mapping)


class FHIRBundledPatientSerializer(serializers.Serializer):
    # full_url = serializers.CharField()
    resource = FHIRPatientSerializer(required=False, read_only=True, source="*")
