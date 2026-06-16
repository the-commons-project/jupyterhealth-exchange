import base64

from rest_framework import serializers

from core.models import PractitionerClient


class PractitionerClientSerializer(serializers.ModelSerializer):
    # `label` is the only writable field (used on create and partial update); everything
    # else in the representation is derived from the linked OAuth Application.
    label = serializers.CharField(required=False, allow_blank=True, default="")

    class Meta:
        model = PractitionerClient
        fields = ["id", "label"]

    def to_representation(self, instance):
        application = instance.application
        # API Key handed to the practitioner: base64(client_id:client_secret). The secret is
        # stored unhashed on these applications (hash_client_secret=False) so it stays readable.
        raw = f"{application.client_id}:{application.client_secret}".encode()
        return {
            "id": instance.id,
            "label": instance.label,
            "created": application.created,
            "name": application.name,
            "key": base64.b64encode(raw).decode(),
        }
