from rest_framework import serializers

from core.models import PatientInvitation


class PatientInvitationSerializer(serializers.ModelSerializer):
    token = serializers.SerializerMethodField()

    def get_token(self, obj):
        return getattr(obj, "token", None)

    class Meta:
        model = PatientInvitation
        fields = ["id", "patient_id", "client_id", "token_hash", "token", "status", "last_updated"]
