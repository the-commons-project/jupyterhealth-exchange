from django.conf import settings
from django.db import models


class PractitionerClient(models.Model):
    """A practitioner-owned OAuth client (client-credentials grant).

    Adds JHE-specific data (the owning practitioner and an optional label) on top of a
    Django OAuth Toolkit Application, mirroring how JheClient augments a patient client.
    Deleting the Application cascades to this row (and vice versa via the API).
    """

    application = models.OneToOneField(
        settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
        on_delete=models.CASCADE,
        related_name="practitioner_client",
    )
    practitioner = models.ForeignKey(
        "Practitioner",
        on_delete=models.CASCADE,
        related_name="clients",
    )
    label = models.TextField(blank=True, default="")
