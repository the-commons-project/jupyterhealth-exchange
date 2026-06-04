from rest_framework import serializers

from core.models import Study, StudyClient, StudyDataSource, StudyPatient, StudyPatientScopeConsent, StudyScopeRequest

from .data_source import DataSourceSerializer
from .organization import OrganizationWithoutLineageSerializer


class StudySerializer(serializers.ModelSerializer):
    class Meta:
        model = Study
        fields = ["id", "name", "description", "organization", "icon_url"]


class StudyOrganizationSerializer(serializers.ModelSerializer):
    organization = OrganizationWithoutLineageSerializer(many=False, read_only=True)

    class Meta:
        model = Study
        fields = ["id", "name", "description", "organization", "icon_url"]


class StudyPatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudyPatient
        fields = ["id", "study", "patient"]
        depth = 1


class StudyScopeRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudyScopeRequest
        fields = ["id", "study", "scope_code"]
        depth = 1


class StudyPatientScopeConsentSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudyPatientScopeConsent
        fields = ["id", "study_patient", "scope_code", "consented", "consented_time"]
        depth = 1


class StudyPendingConsentsSerializer(serializers.ModelSerializer):
    organization = OrganizationWithoutLineageSerializer(many=False, read_only=True)
    data_sources = DataSourceSerializer(many=True, read_only=True)
    pending_scope_consents = serializers.JSONField()

    class Meta:
        model = Study
        fields = [
            "id",
            "name",
            "description",
            "organization",
            "data_sources",
            "pending_scope_consents",
        ]


class StudyConsentsSerializer(serializers.ModelSerializer):
    organization = OrganizationWithoutLineageSerializer(many=False, read_only=True)
    data_sources = DataSourceSerializer(many=True, read_only=True)
    scope_consents = serializers.JSONField()

    class Meta:
        model = Study
        fields = [
            "id",
            "name",
            "description",
            "organization",
            "data_sources",
            "scope_consents",
        ]


class StudyClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudyClient
        fields = ["id", "study", "client"]
        depth = 1


class StudyDataSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudyDataSource
        fields = ["id", "study", "data_source"]
        depth = 1
