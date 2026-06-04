import base64
import json

import humps
from django.core.exceptions import BadRequest
from fhir.resources.observation import Observation as FHIRObservation
from rest_framework import serializers

from core.fhir.config import get_resource_mapping
from core.fhir.mapping import build_fhir_resource
from core.models import Observation


class ObservationSerializer(serializers.ModelSerializer):
    patient_name_family = serializers.CharField()
    patient_name_given = serializers.CharField()
    coding_system = serializers.CharField()
    coding_code = serializers.CharField()
    coding_text = serializers.CharField()

    class Meta:
        model = Observation
        fields = [
            "id",
            "subject_patient_id",
            "patient_name_family",
            "patient_name_given",
            "codeable_concept_id",
            "coding_system",
            "coding_code",
            "coding_text",
            "last_updated",
            "value_attachment_data",
        ]


class ObservationWithoutDataSerializer(serializers.ModelSerializer):
    class Meta:
        model = Observation
        fields = ["id", "subject_patient", "codeable_concept", "last_updated"]


class FHIRObservationSerializer(serializers.Serializer):
    """Renders an Observation model instance into a FHIR R5 Observation resource.

    The shape is driven by the data_mapping in jhe/fhir_config.json. The encoding of
    valueAttachment.data to Base64 (per the FHIR Attachment spec) is not expressible in
    the config, so it is applied here after the generic mapping has run.
    """

    def to_representation(self, observation):
        mapping = get_resource_mapping("Observation")
        as_dict = build_fhir_resource(observation, "Observation", mapping, aux_data=observation.aux_fhir_data)
        # valueAttachment.data must be Base64-encoded binary per FHIR; the mapping yields
        # the raw JSON object, so encode it here (mirrors fhir_create's decode path).
        attachment = as_dict.get("valueAttachment")
        if attachment and attachment.get("data") is not None:
            attachment["data"] = base64.b64encode(json.dumps(attachment["data"]).encode("utf-8")).decode("ascii")
        # validate
        try:
            FHIRObservation.parse_obj(humps.camelize(as_dict))
        except Exception as e:
            raise BadRequest(e)
        return as_dict


class FHIRBundledObservationSerializer(serializers.Serializer):
    # TBD: full_url = serializers.CharField()
    resource = FHIRObservationSerializer(required=False, read_only=True, source="*")


class FHIRBundleSerializer(serializers.Serializer):
    _ = serializers.JSONField()
