from oauth2_provider.models import get_application_model
from rest_framework import serializers

from core.models import ClientDataSource, JheClient

Application = get_application_model()


# !!! NB: weird stuff is going on here with how djangorestframework-camel-case selectively transforms some fields but not all
# Do not make any changes without manual testing
class ClientSerializer(serializers.ModelSerializer):
    clientId = serializers.CharField(source="client_id", required=False)
    invitationUrl = serializers.CharField(
        source="invitation_url", required=False, allow_blank=True, allow_null=True, write_only=True
    )

    class Meta:
        model = Application
        # expose camelCase fields to the client
        fields = ["id", "name", "clientId", "invitationUrl"]

    def to_representation(self, instance):
        data = {
            "id": instance.id,
            "name": instance.name,
            "clientId": instance.client_id,
        }

        jhe_client = getattr(instance, "jhe_client", None)
        data["invitationUrl"] = jhe_client.invitation_url if jhe_client else None

        return data

    def create(self, validated_data):
        print("validated_data keys:", sorted(validated_data.keys()))
        invitation_url = validated_data.pop("invitation_url", None)
        if invitation_url is None:
            invitation_url = self.initial_data.get("invitation_url")

        app = super().create(validated_data)

        # A patient client always gets its JheClient created explicitly here (there is no
        # longer a post_save signal that auto-creates one for every Application).
        jhe_client, _ = JheClient.objects.get_or_create(application=app)
        if invitation_url is not None:
            jhe_client.invitation_url = invitation_url
            jhe_client.save()

        return app

    def update(self, instance, validated_data):
        invitation_url = validated_data.pop("invitation_url", None)
        if invitation_url is None:
            invitation_url = self.initial_data.get("invitation_url")

        app = super().update(instance, validated_data)

        if invitation_url is not None:
            jhe_client, _ = JheClient.objects.get_or_create(application=app)
            jhe_client.invitation_url = invitation_url
            jhe_client.save()

        return app


class ClientDataSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientDataSource
        fields = ["id", "client_id", "data_source_id"]
        depth = 1
