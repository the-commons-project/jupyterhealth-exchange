from urllib.parse import urlparse

from django.conf import settings
from django.db import models
from django.db.utils import IntegrityError
from django.shortcuts import get_object_or_404

from core.jhe_settings.service import get_setting

from .codeable_concept import CodeableConcept
from .practitioner import Practitioner


class Patient(models.Model):
    jhe_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="patient_profile",
        null=True,  # allows pre-existing patients without a JHE user,
        blank=True,
    )
    identifier = models.CharField(null=True)
    name_family = models.CharField(null=True)
    name_given = models.CharField(null=True)
    birth_date = models.DateField(null=True)
    telecom_phone = models.CharField(null=True)
    last_updated = models.DateTimeField(auto_now=True)
    organizations = models.ManyToManyField("Organization", through="PatientOrganization", related_name="patients")

    def __str__(self):
        return f"{self.name_family}, {self.name_given}"

    def consolidated_consented_scopes(self):
        # Return the distinct scope codes this patient has actively consented to, across every
        # study they are enrolled in. The traversal walks CodeableConcept ->
        # StudyPatientScopeConsent -> StudyPatient -> Patient, keeping only consent rows whose
        # "consented" flag is true; both lookups share the "studypatientscopeconsent" prefix
        # and live in one filter() call so they match the SAME consent row. distinct()
        # collapses the duplicate code rows produced when the same scope is consented across
        # multiple studies.
        return CodeableConcept.objects.filter(
            studypatientscopeconsent__consented=True,
            studypatientscopeconsent__study_patient__patient=self,
        ).distinct()

    @staticmethod
    def for_practitioner_organization_study(
        jhe_user_id,
        organization_id=None,
        study_id=None,
        patient_id=None,
        patient_identifier_value=None,
    ):
        # Return the patients a practitioner is allowed to see: every patient who shares
        # an organization with the practitioner identified by jhe_user_id. The traversal
        # walks Patient -> PatientOrganization -> Organization -> PractitionerOrganization
        # -> Practitioner -> JheUser, so a patient matches only when some organization
        # they belong to also has the practitioner as a member. The organization
        # membership of the patient and the practitioner are matched against the SAME
        # organization by keeping both lookups in one filter() call (Django reuses the
        # join for lookups sharing the "organizations" prefix within a single filter);
        # an optional organization_id narrows that shared organization. The result is then
        # optionally narrowed to patients enrolled in a given study, to a single patient by
        # id, or to a patient with a matching identifier. distinct() collapses the duplicate
        # patient rows produced by spanning these many-to-many relationships.
        organization_filters = {"organizations__practitioners__jhe_user_id": jhe_user_id}
        if organization_id:
            organization_filters["organizations__id"] = organization_id

        qs = Patient.objects.filter(**organization_filters)
        if study_id:
            qs = qs.filter(studypatient__study_id=study_id)
        if patient_id:
            qs = qs.filter(id=patient_id)
        if patient_identifier_value:
            qs = qs.filter(identifier=patient_identifier_value)
        return qs.distinct()

    @staticmethod
    def construct_invitation_link(invitation_url, client_id, auth_code):
        site_url = get_setting("site.url", settings.SITE_URL)
        # Use netloc (host:port) instead of hostname (host only) so the
        # consuming app can reach JHE on non-standard ports (e.g. localhost:8000).
        parsed = urlparse(site_url)
        host = parsed.netloc or parsed.hostname
        invitation_code = f"{host}~{client_id}~{auth_code}"
        return invitation_url.replace("CODE", invitation_code)

    @staticmethod
    def practitioner_authorized(
        jhe_user_id,
        patient_id=None,
        patient_identifier_system=None,
        patient_identifier_value=None,
        organization_id=None,
    ):
        return Patient.for_practitioner_organization_study(
            jhe_user_id,
            organization_id,
            None,
            patient_id,
            patient_identifier_value,
        ).exists()

    @staticmethod
    def for_study(jhe_user_id, study_id):
        # Return the patients enrolled in a given study, but only when the practitioner
        # identified by jhe_user_id is authorized for that study. Authorization here flows
        # through the study's own organization (not the patient's): the traversal walks
        # Patient -> StudyPatient -> Study -> Organization -> PractitionerOrganization ->
        # Practitioner -> JheUser. Both lookups share the "studypatient__study" prefix and
        # live in one filter() call so Django reuses the join, ensuring the enrolled study
        # and the practitioner-authorized study are the SAME study. distinct() collapses the
        # duplicate patient rows produced by spanning these many-to-many relationships.
        return Patient.objects.filter(
            studypatient__study_id=study_id,
            studypatient__study__organization__practitioners__jhe_user_id=jhe_user_id,
        ).distinct()

    @staticmethod
    def from_jhe_user_id(jhe_user_id):
        return Patient.objects.get(jhe_user_id=jhe_user_id)

    # GET /Patient?_has:Group:member:_id=<group-id>
    @staticmethod
    def fhir_search(
        jhe_user_id,
        study_id=None,
        patient_identifier_system=None,
        patient_identifier_value=None,
    ):
        practitioner = get_object_or_404(Practitioner, jhe_user_id=jhe_user_id)
        practitioner_id = practitioner.id

        # Explicitly cast to ints so no injection vulnerability
        study_sql_where = ""
        if study_id:
            study_sql_where = f"AND core_studypatient.study_id={int(study_id)}"

        patient_identifier_value_sql_where = ""
        if patient_identifier_value:
            patient_identifier_value_sql_where = "AND core_patient.identifier=%(patient_identifier_value)s"

        # TBD: Query optimization: https://stackoverflow.com/a/6037376
        # TBD: sub constants from config
        q = """
            SELECT  'Patient' as resource_type,
                    core_patient.id as id,
                    core_patient.id::varchar as id_string,
                    -- ('{SITE_URL}/fhir/r5/Patient/' || core_patient.id) as full_url,

                    json_build_object(
                        'last_updated', core_patient.last_updated
                    )::jsonb as meta,

                    json_build_array(
                        json_build_object(
                            'value', core_patient.identifier,
                            'system', 'http://tcp.org'
                        )
                    )::jsonb as identifier,

                    json_build_array(
                        json_build_object(
                            'family', core_patient.name_family,
                            'given',    json_build_array(
                                            core_patient.name_given
                                        )
                        )
                    )::jsonb as name,

                    core_patient.birth_date as birth_date,

                    json_build_array(
                        json_build_object(
                            'value', patient_user.email,
                            'system', 'email'
                        ),
                        json_build_object(
                            'value', core_patient.telecom_phone,
                            'system', 'phone'
                        )
                    )::jsonb as telecom

            FROM core_patient
            JOIN core_jheuser AS patient_user ON patient_user.id=core_patient.jhe_user_id
            JOIN core_studypatient ON core_studypatient.patient_id=core_patient.id
            JOIN core_patientorganization
              ON core_patientorganization.patient_id=core_patient.id
            JOIN core_organization
              ON core_organization.id=core_patientorganization.organization_id
            JOIN core_practitionerorganization
            ON core_practitionerorganization.organization_id=core_organization.id
            WHERE core_practitionerorganization.practitioner_id = %(practitioner_id)s

            {study_sql_where}
            {patient_identifier_value_sql_where}
            ORDER BY core_patient.name_family
            """.format(
            SITE_URL=get_setting("site.url", settings.SITE_URL),
            study_sql_where=study_sql_where,
            patient_identifier_value_sql_where=patient_identifier_value_sql_where,
        )

        records = Patient.objects.raw(
            q,
            {
                "practitioner_id": practitioner_id,
                "patient_identifier_value": patient_identifier_value,
            },
        )
        return records

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if hasattr(self, "_organization_id") and self._organization_id:
            try:
                PatientOrganization.objects.get_or_create(patient=self, organization_id=self._organization_id)
            except IntegrityError as e:
                print(f"IntegrityError: {e}")

    def __init__(self, *args, **kwargs):
        # Remove organization_id if it's passed in, as it should be handled by the M2M relationship
        self._organization_id = None
        if "organization_id" in kwargs:
            self._organization_id = kwargs.pop("organization_id")
        super().__init__(*args, **kwargs)
        self.telecom_email = None


class PatientOrganization(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="organization_links")
    organization = models.ForeignKey("Organization", on_delete=models.CASCADE, related_name="patient_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["patient_id", "organization_id"],
                name="core_patientorganization_unique_patient_id_organization_id",
            )
        ]


class PatientIdentifier(models.Model):
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="identifiers", db_index=True)
    system = models.CharField(db_index=True)
    value = models.CharField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["system", "value"],
                name="core_patientidentifier_unique_system_value",
            )
        ]
