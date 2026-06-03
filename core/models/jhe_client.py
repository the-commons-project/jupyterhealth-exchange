from django.conf import settings
from django.db import models


class JheClient(models.Model):
    application = models.OneToOneField(
        settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
        on_delete=models.CASCADE,
        related_name="jhe_client",
    )
    invitation_url = models.TextField(blank=True, default="")
    aux_data = models.JSONField(null=True, blank=True)
