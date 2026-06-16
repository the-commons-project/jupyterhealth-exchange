import secrets
import string

from django.utils import timezone
from oauth2_provider.models import get_application_model
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from core.models import PractitionerClient
from core.serializers import PractitionerClientSerializer

Application = get_application_model()

# Length of the credentials we generate for practitioner clients. DOT's defaults (40-char
# client_id, 128-char secret) make for an unwieldy ~226-char base64 API key, so we generate
# shorter ones here; a 32-char secret over a 62-char alphabet is still ~190 bits of entropy.
_CLIENT_ID_LENGTH = 16
_CLIENT_SECRET_LENGTH = 32
# ascii_letters + digits only: no ":" so it can't collide with the client_id:client_secret
# separator used to build the base64 API key.
_ALPHABET = string.ascii_letters + string.digits


def _generate_token(length):
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


class IsPractitioner(permissions.IsAuthenticated):
    """Authenticated and a practitioner. Self-ownership of the PractitionerClient is
    additionally enforced by the viewset queryset (only the caller's own rows are
    visible, so READ/UPDATE/DELETE on someone else's client 404s)."""

    message = "Only practitioner users can manage practitioner clients."

    def has_permission(self, request, view):
        return bool(super().has_permission(request, view) and request.user.is_practitioner())


class PractitionerClientViewSet(ModelViewSet):
    serializer_class = PractitionerClientSerializer
    permission_classes = [IsPractitioner]
    # No PUT: the only mutable field is `label`, handled via PATCH.
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        # Self-operations only: a practitioner sees and acts on their own clients exclusively.
        return (
            PractitionerClient.objects.filter(practitioner=self.request.user.practitioner)
            .select_related("application")
            .order_by("-application__created")
        )

    def create(self, request, *args, **kwargs):
        practitioner = request.user.practitioner
        label = request.data.get("label", "") or ""

        # Minimal confidential client-credentials Application. We generate shorter
        # client_id/client_secret than DOT's defaults to keep the base64 API key workable.
        # hash_client_secret=False keeps the secret readable so the base64 API key can be
        # returned on every read (see practitioner_clients.md).
        timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
        application = Application.objects.create(
            user=request.user,
            name=f"_practitioner_client_{practitioner.id}_{timestamp}",
            client_id=_generate_token(_CLIENT_ID_LENGTH),
            client_secret=_generate_token(_CLIENT_SECRET_LENGTH),
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            hash_client_secret=False,
            skip_authorization=True,
        )
        practitioner_client = PractitionerClient.objects.create(
            application=application,
            practitioner=practitioner,
            label=label,
        )

        serializer = self.get_serializer(practitioner_client)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        # Delete the OAuth Application; the OneToOne cascade removes the PractitionerClient.
        instance.application.delete()
