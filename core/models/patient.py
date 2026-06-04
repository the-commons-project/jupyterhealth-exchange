from urllib.parse import urlparse

from django.conf import settings
from django.db import models
from django.db.utils import IntegrityError
from django.shortcuts import get_object_or_404

from core.services.jhe_settings import get_setting

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
    name_family = models.CharField(null=True)
    name_given = models.CharField(null=True)
    birth_date = models.DateField(null=True)
    telecom_phone = models.CharField(null=True)
    aux_fhir_data = models.JSONField(null=True)
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
            qs = qs.filter(identifiers__value=patient_identifier_value)
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
        # Return the patients a practitioner may see via the FHIR API as a queryset of
        # Patient instances; formatting into FHIR JSON is the serializer's job. A patient
        # qualifies only when enrolled in some study (studypatient) AND sharing an
        # organization with the practitioner. jhe_user is selected and identifiers are
        # prefetched so the serializer's data-mapping traversal does not issue N+1 queries.
        practitioner = get_object_or_404(Practitioner, jhe_user_id=jhe_user_id)

        qs = Patient.objects.filter(
            organizations__practitioners=practitioner,
            studypatient__isnull=False,
        )
        if study_id:
            qs = qs.filter(studypatient__study_id=study_id)
        if patient_identifier_value:
            qs = qs.filter(identifiers__value=patient_identifier_value)

        return qs.select_related("jhe_user").prefetch_related("identifiers").distinct().order_by("name_family")

    @staticmethod
    def fhir_create(data, user):
        # Create a Patient from a FHIR resource. The config-mapped fields (name, birthDate)
        # are reverse-mapped onto columns by the engine; everything the config does not claim
        # is preserved in aux_fhir_data (the inverse of the read path). Telecom and identifiers
        # are handled here because they cannot be inverted declaratively: the email drives the
        # linked JheUser (mirroring the admin create path) and identifiers fan out to rows.
        import humps
        from django.core.exceptions import BadRequest, PermissionDenied
        from django.utils.crypto import get_random_string
        from fhir.resources.patient import Patient as FHIRPatient

        from core.fhir.config import get_resource_mapping
        from core.fhir.engine import split_resource

        from .jhe_user import JheUser

        if not user.is_practitioner():
            raise PermissionDenied("Only practitioners may create Patients via FHIR.")

        camelized = humps.camelize(data)
        try:
            FHIRPatient.parse_obj(camelized)
        except Exception as e:
            raise BadRequest(e)

        columns, aux_fhir_data = split_resource(camelized, "Patient", get_resource_mapping("Patient"))

        # Telecom is a multi-template list the engine cannot invert; pull phone/email out here.
        email = None
        for contact in camelized.get("telecom", []) or []:
            if contact.get("value") and contact.get("system") == "email":
                email = contact["value"]
            elif contact.get("value") and contact.get("system") == "phone":
                columns["telecom_phone"] = contact["value"]

        jhe_user = None
        if email:
            jhe_user = JheUser.objects.filter(email=email).first()
            if jhe_user is None:
                jhe_user = JheUser(email=email)
                jhe_user.set_password(get_random_string(length=16))
                jhe_user.save()

        editable = {f.name for f in Patient._meta.concrete_fields if f.editable and not f.primary_key}
        field_values = {name: value for name, value in columns.items() if name in editable}

        patient = Patient(jhe_user=jhe_user, aux_fhir_data=aux_fhir_data or None, **field_values)
        if organization_id := user.practitioner_profile.get_setting("current_organization_id"):
            patient._organization_id = organization_id
        patient.save()

        for identifier in camelized.get("identifier", []) or []:
            system = identifier.get("system")
            value = identifier.get("value")
            if system and value:
                PatientIdentifier.objects.create(patient=patient, system=system, value=value)

        return patient

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
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="identifiers")
    system = models.CharField(db_index=True)
    value = models.CharField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["system", "value"],
                name="core_patientidentifier_unique_system_value",
            )
        ]
