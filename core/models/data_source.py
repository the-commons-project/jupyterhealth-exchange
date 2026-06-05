from django.conf import settings
from django.db import models

from .codeable_concept import CodeableConcept


class DataSource(models.Model):
    """
    FHIR Device
    """

    DATA_SOURCE_TYPES = {
        "medical_device": "Medical Device",
        "personal_device": "Personal Device",
        "patient_app": "Patient App",
    }
    name = models.CharField(null=True, blank=True)
    type = models.CharField(
        choices=list(DATA_SOURCE_TYPES.items()),
        null=False,
        blank=False,
        default="personal_device",
    )
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name or f"DataSource {self.pk}"

    # this will never be large
    @staticmethod
    def data_sources_with_scopes(data_source_id=None, study_id=None):
        # Return the data sources (optionally a single one by id, or only those attached to a
        # given study), ordered by name. The study filter walks DataSource -> StudyDataSource
        # -> Study via the "studydatasource" reverse relation, so a data source matches only
        # when it is linked to that study. For each returned data source we then attach its
        # supported_scopes: the CodeableConcept codes reachable through the
        # DataSourceSupportedScope join table (CodeableConcept -> DataSourceSupportedScope ->
        # DataSource), ordered by text. Those scopes are fetched with one extra query per data
        # source, which is acceptable because the set of data sources is small.
        data_sources = DataSource.objects.all()
        if data_source_id:
            data_sources = data_sources.filter(id=data_source_id)
        elif study_id:
            data_sources = data_sources.filter(studydatasource__study_id=study_id)
        data_sources = list(data_sources.order_by("name"))

        for data_source in data_sources:
            data_source.supported_scopes = list(
                CodeableConcept.objects.filter(datasourcesupportedscope__data_source_id=data_source.id).order_by("text")
            )

        return data_sources

    @staticmethod
    def fhir_search(jhe_user_id, device_id=None, is_patient=False):
        # Return the Devices (DataSources) a user may see via the FHIR API as a queryset of
        # DataSource instances; the serializer renders them. A practitioner sees devices linked
        # (via StudyDataSource) to studies under an organization they belong to; a patient sees
        # devices linked to studies they are enrolled in. distinct() collapses the duplicate
        # rows produced by spanning these many-to-many relationships.
        if is_patient:
            qs = DataSource.objects.filter(studydatasource__study__studypatient__patient__jhe_user_id=jhe_user_id)
        else:
            qs = DataSource.objects.filter(studydatasource__study__organization__practitioners__jhe_user_id=jhe_user_id)
        if device_id:
            qs = qs.filter(id=device_id)
        return qs.distinct().order_by("name")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.supported_scopes = []


class DataSourceSupportedScope(models.Model):
    data_source = models.ForeignKey(DataSource, on_delete=models.CASCADE)
    scope_code = models.ForeignKey("CodeableConcept", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["data_source", "scope_code"],
                name="core_datasourcesupportedscope_unique_data_source_id_scope_code_id",
            )
        ]


class ClientDataSource(models.Model):
    client = models.ForeignKey(
        settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
        on_delete=models.CASCADE,
        related_name="data_sources",
    )
    data_source = models.ForeignKey(
        DataSource,
        on_delete=models.CASCADE,
        related_name="client_applications",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["client", "data_source"],
                name="core_clientdatasource_unique_client_id_data_source_id",
            )
        ]
