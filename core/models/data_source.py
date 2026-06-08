from django.conf import settings
from django.db import models

from core.fhir.scope import authorize_practitioner_scope, resolve_fhir_user

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
    def fhir_search(
        jhe_user_id,
        resource_id=None,
        organization_id=None,
        study_id=None,
        patient_id=None,
        **params,
    ):
        # Return the Devices (DataSources) visible to the user as a queryset of DataSource
        # instances (the serializer renders them into FHIR JSON). A patient user sees the
        # devices used in the studies they are enrolled in and the organization/study/patient
        # filters are ignored; a practitioner sees the devices linked (via StudyDataSource) to
        # studies under an organization they belong to -- narrowed to one organization, a
        # single study, or the studies a given patient is part of (each explicit filter
        # authorized up front, 403 on mismatch). resource_id selects a single device.
        # distinct() collapses the duplicate rows produced by spanning these many-to-many
        # relationships.
        user = resolve_fhir_user(jhe_user_id)
        if user.is_patient():
            qs = DataSource.objects.filter(studydatasource__study__studypatient__patient__jhe_user_id=jhe_user_id)
        else:
            authorize_practitioner_scope(jhe_user_id, organization_id, study_id, patient_id)
            qs = DataSource.objects.filter(studydatasource__study__organization__practitioners__jhe_user_id=jhe_user_id)
            if organization_id:
                qs = qs.filter(studydatasource__study__organization_id=organization_id)
            if study_id:
                qs = qs.filter(studydatasource__study_id=study_id)
            if patient_id:
                qs = qs.filter(studydatasource__study__studypatient__patient_id=patient_id)

        if resource_id:
            qs = qs.filter(id=resource_id)

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
