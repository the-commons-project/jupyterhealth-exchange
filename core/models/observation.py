import base64
import json
import logging

from django.conf import settings
from django.core.exceptions import BadRequest, PermissionDenied
from django.db import models
from django.db.models import F, Q
from django.db.utils import IntegrityError
from django.shortcuts import get_object_or_404
from fhir.resources.observation import Observation as FHIRObservation
from jsonschema import ValidationError

from core.jhe_settings.service import get_setting
from core.utils import validate_with_registry

from .codeable_concept import CodeableConcept
from .data_source import DataSource
from .patient import Patient
from .practitioner import Practitioner

logger = logging.getLogger(__name__)


# Observation per record: https://stackoverflow.com/a/61484800 (author worked at ONC)
class Observation(models.Model):
    subject_patient = models.ForeignKey("Patient", on_delete=models.CASCADE)
    codeable_concept = models.ForeignKey("CodeableConcept", on_delete=models.CASCADE)
    data_source = models.ForeignKey("DataSource", on_delete=models.SET_NULL, null=True)
    value_attachment_data = models.JSONField()
    last_updated = models.DateTimeField(auto_now=True)
    ow_key = models.CharField(max_length=512, null=True, blank=True, db_index=True)

    # https://build.fhir.org/valueset-observation-status.html
    OBSERVATION_STATUSES = {
        "registered": "registered",
        "preliminary": "preliminary",
        "final": "final",
        "amended": "amended",
        "corrected": "corrected",
        "appended": "appended",
        "cancelled": "cancelled",
        "entered-in-error": "Entered in Error",
        "unknown": "Unknown",
    }

    status = models.CharField(choices=list(OBSERVATION_STATUSES.items()), null=False, blank=False, default="final")

    @staticmethod
    def for_practitioner_organization_study_patient(
        jhe_user_id,
        organization_id=None,
        study_id=None,
        patient_id=None,
        observation_id=None,
    ):
        practitioner = get_object_or_404(Practitioner, jhe_user_id=jhe_user_id)

        # Return the observations a practitioner is allowed to see, newest first. An
        # observation is visible only when its patient shares an organization with the
        # practitioner identified by jhe_user_id: the traversal walks Observation -> Patient
        # -> PatientOrganization -> Organization -> PractitionerOrganization -> Practitioner.
        # Keeping the patient's and the practitioner's organization lookups in one filter()
        # call matches them against the SAME organization (Django reuses the join for lookups
        # sharing the "subject_patient__organizations" prefix); an optional organization_id
        # narrows that shared organization. The result is then optionally narrowed: by study
        # (the patient must be enrolled in the study AND the observation's code must be one of
        # that study's requested scopes, both matched against the SAME study), to a single
        # patient, or to a single observation. The annotate() calls flatten columns from the
        # joined CodeableConcept and Patient rows onto each observation as plain attributes for
        # the serializer to read. distinct() collapses the duplicate observation rows produced
        # by spanning these many-to-many relationships.
        organization_filters = {"subject_patient__organizations__practitioners": practitioner}
        if organization_id:
            organization_filters["subject_patient__organizations__id"] = organization_id
        qs = Observation.objects.filter(**organization_filters)

        if study_id:
            qs = qs.filter(
                subject_patient__studypatient__study_id=study_id,
                codeable_concept__studyscoperequest__study_id=study_id,
            )
        if patient_id:
            qs = qs.filter(subject_patient_id=patient_id)
        if observation_id:
            qs = qs.filter(id=observation_id)

        return (
            qs.annotate(
                coding_system=F("codeable_concept__coding_system"),
                coding_code=F("codeable_concept__coding_code"),
                coding_text=F("codeable_concept__text"),
                patient_name_family=F("subject_patient__name_family"),
                patient_name_given=F("subject_patient__name_given"),
            )
            .distinct()
            .order_by("-last_updated")
        )

    @staticmethod
    def practitioner_authorized(practitioner_user_id, observation_id):
        return Observation.for_practitioner_organization_study_patient(
            practitioner_user_id, None, None, None, observation_id
        ).exists()

    @staticmethod
    def fhir_search(
        jhe_user_id,
        study_id=None,
        patient_id=None,
        patient_identifier_system=None,
        patient_identifier_value=None,
        coding_system=None,
        coding_code=None,
        observation_id=None,
    ):
        practitioner = get_object_or_404(Practitioner, jhe_user_id=jhe_user_id)
        practitioner_id = practitioner.id

        # Explicitly cast to ints so no injection vulnerability
        study_sql_where = ""
        study_scope_join = ""
        study_scope_where = ""
        if study_id:
            study_sql_where = f"AND core_study.id={int(study_id)}"
            study_scope_join = "JOIN core_studyscoperequest ON core_studyscoperequest.study_id=core_study.id"
            study_scope_where = "AND core_observation.codeable_concept_id=core_studyscoperequest.scope_code_id"

        patient_id_sql_where = ""
        if patient_id:
            patient_id_sql_where = f"AND core_patient.id={int(patient_id)}"

        patient_identifier_value_sql_where = ""
        if patient_identifier_value:
            patient_identifier_value_sql_where = "AND core_patient.identifier=%(patient_identifier_value)s"

        observation_sql_where = ""
        if observation_id:
            observation_sql_where = f"AND core_observation.id={int(observation_id)}"

        # TBD: Query optimization: https://stackoverflow.com/a/6037376
        # pagination: https://github.com/mattbuck85/django-paginator-rawqueryset
        q = """
            SELECT  'Observation' as resource_type,
                    'final' as status,
                    core_observation.id as id,
                    core_observation.id::varchar as id_string,
                    -- ('{SITE_URL}/fhir/r5/Observation/' || core_observation.id) as full_url,

                    json_build_object(
                        'last_updated',
                        core_observation.last_updated
                    )::jsonb as meta,

                                                                      -- double bracket for python .format ignore
                    jsonb_agg(to_jsonb(core_observationidentifier) - '{{id, observation_id}}'::text[]) as identifier,

                    json_build_object(
                        'reference',
                        'Patient/' || core_observation.subject_patient_id
                    )::jsonb as subject,

                    json_build_object(
                        'coding',
                        json_build_array(
                            json_build_object(
                                'system', core_codeableconcept.coding_system,
                                'code', core_codeableconcept.coding_code
                            )
                        )
                    )::jsonb as code,

                    json_build_object(
                        'content_type',
                        'application/json',
                        'data',
                        encode(convert_to(core_observation.value_attachment_data::text, 'UTF-8'), 'base64')
                    )::jsonb as value_attachment

            FROM core_observation
            LEFT JOIN core_observationidentifier ON core_observationidentifier.observation_id=core_observation.id
            JOIN core_codeableconcept ON core_codeableconcept.id=core_observation.codeable_concept_id
            JOIN core_patient ON core_patient.id=core_observation.subject_patient_id
            JOIN core_patientorganization ON core_patientorganization.patient_id=core_patient.id
            JOIN core_organization ON core_organization.id=core_patientorganization.organization_id
            JOIN core_practitionerorganization ON core_practitionerorganization.organization_id=core_organization.id
            LEFT JOIN core_studypatient ON core_studypatient.patient_id=core_patient.id
            LEFT JOIN core_study ON core_study.id=core_studypatient.study_id
            {study_scope_join}
            WHERE core_practitionerorganization.practitioner_id = %(practitioner_id)s
            AND core_codeableconcept.coding_system LIKE %(coding_system)s AND core_codeableconcept.coding_code LIKE %(coding_code)s

            {study_sql_where}
            {study_scope_where}
            {patient_id_sql_where}
            {patient_identifier_value_sql_where}
            {observation_sql_where}
            GROUP BY core_observation.id, core_codeableconcept.coding_system, core_codeableconcept.coding_code
            ORDER BY core_observation.last_updated DESC
            """.format(
            SITE_URL=get_setting("site.url", settings.SITE_URL),
            study_sql_where=study_sql_where,
            study_scope_join=study_scope_join,
            study_scope_where=study_scope_where,
            patient_id_sql_where=patient_id_sql_where,
            patient_identifier_value_sql_where=patient_identifier_value_sql_where,
            observation_sql_where=observation_sql_where,
        )

        return Observation.objects.raw(
            q,
            {
                "practitioner_id": practitioner_id,
                "coding_system": coding_system if coding_system else "%",
                "coding_code": coding_code if coding_code else "%",
                "patient_identifier_value": patient_identifier_value,
            },
        )

    # Get the binary data eg https://www.rapidtables.com/convert/number/string-to-binary.html (delimiter=none)
    # base64 it eg https://cryptii.com/pipes/binary-to-base64
    @staticmethod
    def fhir_create(data, user):
        # Validate Structure
        fhir_observation = None
        try:
            import humps

            fhir_observation = FHIRObservation.parse_obj(humps.camelize(data))
        except Exception as e:
            raise (BadRequest(e))  # TBD: move to view

        # Check Patient
        subject_patient = None
        if (
            not fhir_observation.subject
            or not fhir_observation.subject.reference
            or not fhir_observation.subject.reference.startswith("Patient/")
        ):
            raise (
                BadRequest("Subject is required and must be a reference to a Patient ID and start with 'Patient/'")
            )  # TBD: move to view
        subject_patient_id = fhir_observation.subject.reference.split("/")[1]
        try:
            subject_patient = Patient.objects.get(pk=subject_patient_id)
        except Patient.DoesNotExist:
            raise (BadRequest(f"Patient id={subject_patient_id} can not be found."))  # TBD: move to view

        if user.is_practitioner():
            if not subject_patient.practitioner_authorized(user.pk, subject_patient.id):
                raise PermissionDenied("Current user doesn't have access to the Patient.")
            user_patient = subject_patient
        else:
            user_patient = user.get_patient()
        if user_patient is None:
            raise PermissionDenied("Current user is not a Patient.")

        if user_patient and (subject_patient.id != user_patient.id):
            raise PermissionDenied("The Subject Patient does not match the current user.")

        # Check Identifiers
        if fhir_observation.identifier:
            for identifier in fhir_observation.identifier:
                existing_ids = ObservationIdentifier.objects.filter(system=identifier.system, value=identifier.value)
                if len(existing_ids) > 0:
                    raise IntegrityError(
                        f"Identifier already exists: system={identifier.system} value={identifier.value}"
                    )

        # Check Device
        data_source = None
        if (
            not fhir_observation.device
            or not fhir_observation.device.reference
            or not fhir_observation.device.reference.startswith("Device/")
        ):
            raise (
                BadRequest("Device is required and must be a reference to a Data Source ID and start with 'Device/'")
            )  # TBD: move to view
        device_id = fhir_observation.device.reference.split("/")[1]
        try:
            data_source = DataSource.objects.get((Q(type="personal_device") | Q(type="device")), id=device_id)
        except DataSource.DoesNotExist:
            raise (BadRequest(f"Device Data Source id={device_id} can not be found."))  # TBD: move to view

        # Check Scope
        if len(fhir_observation.code.coding) == 0 or len(fhir_observation.code.coding) > 1:
            raise BadRequest("Exactly one Code must be provided.")  # TBD: move to view

        codeable_concepts = CodeableConcept.objects.filter(
            coding_system=fhir_observation.code.coding[0].system,
            coding_code=fhir_observation.code.coding[0].code,
        )

        if len(codeable_concepts) == 0:
            raise BadRequest(
                f"Code not found: system={fhir_observation.code.coding[0].system} code={fhir_observation.code.coding[0].code}"  # TBD: move to view
            )

        if codeable_concepts[0].id not in [scope.id for scope in user_patient.consolidated_consented_scopes()]:
            raise PermissionDenied(
                f"Observation data with coding_system={codeable_concepts[0].coding_system} coding_code={codeable_concepts[0].coding_code} has not been consented"
                " for any studies by this Patient."
            )

        try:
            value_attachment_data_binary = base64.b64decode(fhir_observation.valueAttachment.data)
            value_attachment_data_json = value_attachment_data_binary.decode("ascii")
            value_attachment_data = json.loads(value_attachment_data_json)
        except Exception:
            raise BadRequest("valueAttachment.data must be Base 64 Encoded Binary JSON.")  # TBD: move to view

        observation = Observation.objects.create(
            subject_patient=subject_patient,
            data_source=data_source,
            codeable_concept=codeable_concepts[0],
            status=fhir_observation.status,
            value_attachment_data=value_attachment_data,
            last_updated=models.DateTimeField(auto_now=True),
        )

        if fhir_observation.identifier:
            for identifier in fhir_observation.identifier:
                ObservationIdentifier.objects.create(
                    observation=observation,
                    system=identifier.system,
                    value=identifier.value,
                )

        return observation

    @staticmethod
    def validate_outer_schema(instance_data):
        for name in ("data-point-1.0.json", "data-series-1.0.json"):
            schema = json.loads((settings.DATA_DIR_PATH.schemas_metadata / name).read_text())
            try:
                validate_with_registry(instance=instance_data, schema=schema)
                return True
            except ValidationError:
                # Not a match; try the next outer schema
                continue
        # Neither matched as a valid outer schema
        return False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # FHIR serialization support
        self.identifier = None
        self.resource_type = None
        self.meta = None
        self.value_attachment = None
        self.subject = None
        self.code = None

    def clean(self):
        try:
            value_attachment_data = self.value_attachment_data

            header_schema = json.loads((settings.DATA_DIR_PATH.schemas_metadata / "header-1.0.json").read_text())
            validate_with_registry(instance=value_attachment_data.get("header"), schema=header_schema)

            body_schema = json.loads(
                (
                    settings.DATA_DIR_PATH.schemas_data
                    / f"schema-{self.codeable_concept.coding_code.replace(':', '_').replace('.', '-')}.json"
                ).read_text()
            )
            validate_with_registry(instance=value_attachment_data.get("body"), schema=body_schema)
        except Exception as error:
            raise error

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class ObservationIdentifier(models.Model):
    observation = models.ForeignKey(Observation, on_delete=models.CASCADE)
    system = models.CharField(null=True, blank=False)
    value = models.CharField(null=True, blank=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["system", "value"],
                name="core_observation_identifier_unique_observation_system_value",
            )
        ]
