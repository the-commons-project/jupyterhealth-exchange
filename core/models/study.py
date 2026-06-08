from django.conf import settings
from django.db import models
from django.shortcuts import get_object_or_404

from core.fhir.scope import authorize_practitioner_scope, resolve_fhir_user

from .codeable_concept import CodeableConcept
from .data_source import DataSource
from .practitioner import Practitioner


class Study(models.Model):
    """
    FHIR Group
    """

    name = models.CharField()
    description = models.TextField()
    organization = models.ForeignKey("Organization", on_delete=models.CASCADE)
    patients = models.ManyToManyField("Patient", through="StudyPatient")
    icon_url = models.TextField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name or f"Study {self.pk}"

    @staticmethod
    def for_practitioner_organization(jhe_user_id, organization_id=None, study_id=None):
        practitioner = get_object_or_404(Practitioner, jhe_user_id=jhe_user_id)

        # Return the studies a practitioner is allowed to see, ordered by name. A study matches
        # only when the practitioner identified by jhe_user_id belongs to the study's own
        # organization: the traversal walks Study -> Organization -> PractitionerOrganization
        # -> Practitioner via the "practitioners" reverse relation (which spans the
        # PractitionerOrganization join table). Optional study_id / organization_id narrow the
        # result to a single study or to studies under one organization.
        # select_related("organization") pulls each study's organization in the same query (the
        # raw SQL it replaces selected both tables), and distinct() collapses the duplicate
        # study rows produced by spanning the practitioner many-to-many relationship.
        studies = Study.objects.filter(organization__practitioners=practitioner)
        if study_id:
            studies = studies.filter(id=study_id)
        if organization_id:
            studies = studies.filter(organization_id=organization_id)
        return studies.select_related("organization").distinct().order_by("name")

    @staticmethod
    def practitioner_authorized(practitioner_user_id, study_id):
        qs = Study.for_practitioner_organization(practitioner_user_id, None, study_id)
        return qs.exists()

    @staticmethod
    def fhir_search(
        jhe_user_id,
        resource_id=None,
        organization_id=None,
        study_id=None,
        patient_id=None,
        **params,
    ):
        # Return the Groups (Studies) visible to the user as a queryset of Study instances (the
        # serializer renders them into FHIR JSON). A patient user sees the studies they are
        # enrolled in and the organization/study/patient filters are ignored; a practitioner
        # sees studies under an organization they belong to -- narrowed to one organization, a
        # single study, or the studies a given patient is enrolled in (each explicit filter
        # authorized up front, 403 on mismatch). resource_id selects a single study.
        user = resolve_fhir_user(jhe_user_id)
        if user.is_patient():
            qs = Study.objects.filter(studypatient__patient__jhe_user_id=jhe_user_id)
        else:
            authorize_practitioner_scope(jhe_user_id, organization_id, study_id, patient_id)
            qs = Study.objects.filter(organization__practitioners__jhe_user_id=jhe_user_id)
            if organization_id:
                qs = qs.filter(organization_id=organization_id)
            if study_id:
                qs = qs.filter(id=study_id)
            if patient_id:
                qs = qs.filter(studypatient__patient_id=patient_id)

        if resource_id:
            qs = qs.filter(id=resource_id)

        return qs.select_related("organization").distinct().order_by("name")

    def has_patient(study_id, patient_id):
        study_patients = StudyPatient.objects.filter(study_id=study_id, patient_id=patient_id)
        if len(study_patients) == 0:
            return False
        return True

    @staticmethod
    def studies_with_scopes(patient_id, pending=False):
        # Return the studies a patient is enrolled in, each decorated with the patient's scope
        # consents, for rendering a consent screen. With pending=False each study carries its
        # scope_consents (requested scopes the patient already has a consent row for); with
        # pending=True each study carries its pending_scope_consents (requested scopes the
        # patient has NOT yet been asked about, i.e. no consent row exists). Three queries feed
        # this: every StudyScopeRequest for studies the patient is enrolled in (StudyScopeRequest
        # -> Study -> StudyPatient -> Patient), the patient's StudyPatient rows keyed by study,
        # and the patient's StudyPatientScopeConsent rows keyed by (study_patient, scope_code).
        # They are joined in Python: for each requested scope we look up the matching consent
        # row and, depending on pending, keep either the scopes with a consent row or those
        # without. Each study is also decorated with its data_sources. The set is small, so the
        # per-study work and extra queries are acceptable.
        scope_requests = (
            StudyScopeRequest.objects.filter(study__studypatient__patient_id=patient_id)
            .select_related("scope_code", "study")
            .order_by("study_id", "id")
        )
        study_patient_ids = {sp.study_id: sp.id for sp in StudyPatient.objects.filter(patient_id=patient_id)}
        consents = {
            (c.study_patient_id, c.scope_code_id): c
            for c in StudyPatientScopeConsent.objects.filter(study_patient__patient_id=patient_id)
        }

        study_id_studies_map = {}

        # this will never be large
        for scope_request in scope_requests:
            study_patient_id = study_patient_ids.get(scope_request.study_id)
            consent = consents.get((study_patient_id, scope_request.scope_code_id))
            # pending=True wants scope requests with no consent row; pending=False wants those with one
            if pending != (consent is None):
                continue

            study = study_id_studies_map.get(scope_request.study_id)
            if study is None:
                study = scope_request.study
                study.data_sources = DataSource.data_sources_with_scopes(None, study.id)
                study_id_studies_map[study.id] = study

            code = scope_request.scope_code
            scope_consent = {
                "code": {
                    "id": code.id,
                    "coding_system": code.coding_system,
                    "coding_code": code.coding_code,
                    "text": code.text,
                },
                "consented": consent.consented if consent else None,
                "consented_time": consent.consented_time if consent else None,
            }
            if pending:
                study.pending_scope_consents.append(scope_consent)
            else:
                study.scope_consents.append(scope_consent)

        return list(study_id_studies_map.values())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_scope_consents = []
        self.scope_consents = []
        self.data_sources = []


class StudyPatient(models.Model):
    study = models.ForeignKey(Study, on_delete=models.CASCADE)
    patient = models.ForeignKey("Patient", on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study_id", "patient_id"],
                name="core_studypatient_unique_study_id_patient_id",
            )
        ]


class StudyPatientScopeConsent(models.Model):
    study_patient = models.ForeignKey(StudyPatient, on_delete=models.CASCADE)
    scope_actions = models.CharField(
        null=True,
        blank=False,
        # https://build.fhir.org/ig/HL7/smart-app-launch/scopes-and-launch-context.html#scopes-for-requesting-fhir-resources
        default="rs",
    )
    scope_code = models.ForeignKey("CodeableConcept", on_delete=models.PROTECT)
    consented = models.BooleanField(null=False, blank=False)
    consented_time = models.DateTimeField()

    @staticmethod
    def patient_scopes(jhe_user_id):
        # Return the distinct scope codes the patient identified by jhe_user_id has actively
        # consented to, across all their study enrollments. The traversal walks CodeableConcept
        # -> StudyPatientScopeConsent -> StudyPatient -> Patient -> JheUser, keeping only consent
        # rows whose "consented" flag is true; both lookups share the "studypatientscopeconsent"
        # prefix and live in one filter() call so they match the SAME consent row. distinct()
        # collapses the duplicate code rows produced when the same scope is consented across
        # multiple studies.
        return CodeableConcept.objects.filter(
            studypatientscopeconsent__consented=True,
            studypatientscopeconsent__study_patient__patient__jhe_user_id=jhe_user_id,
        ).distinct()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study_patient", "scope_code"],
                name="core_studypatientscopeconsent_unique_study_patient_id_scope_code_id",
            )
        ]


class StudyScopeRequest(models.Model):
    study = models.ForeignKey(Study, on_delete=models.CASCADE)
    scope_actions = models.CharField(null=True, blank=False, default="rs")
    scope_code = models.ForeignKey("CodeableConcept", on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study", "scope_code"],
                name="core_studyscoperequest_unique_study_id_scope_code_id",
            )
        ]


class StudyDataSource(models.Model):
    study = models.ForeignKey(Study, on_delete=models.CASCADE)
    data_source = models.ForeignKey("DataSource", on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study", "data_source"],
                name="core_studydatasource_unique_study_id_data_source_id",
            )
        ]


class StudyClient(models.Model):
    study = models.ForeignKey(Study, on_delete=models.CASCADE)
    client = models.ForeignKey(
        settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
        on_delete=models.CASCADE,
        related_name="studies",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study", "client"],
                name="core_studyclient_unique_study_id_client_id",
            )
        ]
