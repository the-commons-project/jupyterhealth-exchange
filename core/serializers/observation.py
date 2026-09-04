import base64
import json

from rest_framework import serializers

from core.fhir.config import get_resource_mapping
from core.fhir.engine import build_fhir_resource
from core.models import Observation


class ObservationSerializer(serializers.ModelSerializer):
    # Annotations added by Observation.for_practitioner_organization_study_patient, not model
    # fields, so ModelSerializer cannot build them and they are declared here. read_only
    # because they are projections of joined CodeableConcept / Patient rows: there is no
    # Observation column to write them back to, and a writable field that is not a valid
    # Observation.objects.create() kwarg raises TypeError on any write.
    patient_name_family = serializers.CharField(read_only=True)
    patient_name_given = serializers.CharField(read_only=True)
    jhe_user_id = serializers.IntegerField(read_only=True)
    coding_system = serializers.CharField(read_only=True)
    coding_code = serializers.CharField(read_only=True)
    coding_text = serializers.CharField(read_only=True)

    class Meta:
        model = Observation
        fields = [
            "id",
            "subject_patient_id",
            "patient_name_family",
            "patient_name_given",
            "jhe_user_id",
            "codeable_concept_id",
            "coding_system",
            "coding_code",
            "coding_text",
            "last_updated",
            "omh_data",
        ]


class FHIRObservationSerializer(serializers.Serializer):
    """Renders an Observation model instance into a FHIR R5 Observation resource.

    The shape is driven by the mapping in core/fhir/fhir_config.json. The encoding of
    valueAttachment.data to Base64 (per the FHIR Attachment spec) is not expressible in
    the config, so it is applied here after the generic mapping has run. Output is not
    validated against fhir.resources -- validation happens on the way in
    (Observation.fhir_create), not on the way out.
    """

    def to_representation(self, observation):
        mapping = get_resource_mapping("Observation")
        as_dict = build_fhir_resource(observation, "Observation", mapping)
        # valueAttachment.data must be Base64-encoded binary per FHIR. The mapping yields the
        # raw JSON object from omh_data, so encode it here (mirrors fhir_create's decode path).
        attachment = as_dict.get("valueAttachment")
        if attachment and isinstance(attachment.get("data"), (dict, list)):
            attachment["data"] = base64.b64encode(json.dumps(attachment["data"]).encode("utf-8")).decode("ascii")
        # subject.identifier carries the patient's jheUserId (issue #602). The mapping yields the
        # raw integer id; FHIR Identifier.value is a string, so coerce it here.
        identifier = (as_dict.get("subject") or {}).get("identifier")
        if identifier and "value" in identifier:
            identifier["value"] = str(identifier["value"])
        return as_dict


class FHIRBundledObservationSerializer(serializers.Serializer):
    # TBD: full_url = serializers.CharField()
    resource = FHIRObservationSerializer(required=False, read_only=True, source="*")


class FHIRBundleSerializer(serializers.Serializer):
    _ = serializers.JSONField()
