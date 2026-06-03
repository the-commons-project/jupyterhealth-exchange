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

from core.utils import validate_with_registry

from .codeable_concept import CodeableConcept
from .data_source import DataSource
from .patient import Patient
from .practitioner import Practitioner

logger = logging.getLogger(__name__)


# Observation per record: https://stackoverflow.com/a/61484800 (author worked at ONC)
class Observation(models.Model):
    subject_patient = models.ForeignKey("Patient", on_delete=models.CASCADE)
    codeable_concept = models.ForeignKey("CodeableConcept", on_delete=models.PROTECT)
    data_source = models.ForeignKey("DataSource", on_delete=models.SET_NULL, null=True)
    value_attachment_data = models.JSONField()
    last_updated = models.DateTimeField(auto_now=True)
    ow_key = models.CharField(max_length=512, null=True, blank=True, db_index=True)
    aux_data = models.JSONField(null=True)

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

    class Meta:
        indexes = [
            # Matches the common access pattern: a patient's observations, newest first.
            models.Index(fields=["subject_patient", "-last_updated"]),
        ]

    @property
    def codeable_concepts(self):
        # Each Observation has exactly one CodeableConcept; expose it as a one-element
        # iterable so the data-mapping engine fans it out into the code.coding array.
        return [self.codeable_concept]

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
        # Return the observations a practitioner may see via the FHIR API as a queryset of
        # Observation instances; formatting into FHIR JSON is the serializer's job. An
        # observation is visible only when its patient shares an organization with the
        # practitioner. When a study is given, the patient must be enrolled in it AND the
        # observation's code must be one of that study's requested scopes. Related rows used
        # by the serializer's data-mapping traversal are selected/prefetched to avoid N+1.
        practitioner = get_object_or_404(Practitioner, jhe_user_id=jhe_user_id)

        qs = Observation.objects.filter(subject_patient__organizations__practitioners=practitioner)
        if study_id:
            qs = qs.filter(
                subject_patient__studypatient__study_id=study_id,
                codeable_concept__studyscoperequest__study_id=study_id,
            )
        if patient_id:
            qs = qs.filter(subject_patient_id=patient_id)
        if patient_identifier_value:
            qs = qs.filter(subject_patient__identifiers__value=patient_identifier_value)
        if coding_system:
            qs = qs.filter(codeable_concept__coding_system=coding_system)
        if coding_code:
            qs = qs.filter(codeable_concept__coding_code=coding_code)
        if observation_id:
            qs = qs.filter(id=observation_id)

        return (
            qs.select_related("subject_patient", "codeable_concept")
            .prefetch_related("identifiers")
            .distinct()
            .order_by("-last_updated")
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
    observation = models.ForeignKey(Observation, on_delete=models.CASCADE, related_name="identifiers")
    system = models.CharField(null=True, blank=True)
    value = models.CharField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["system", "value"],
                name="core_observationidentifier_unique_system_value",
            )
        ]
