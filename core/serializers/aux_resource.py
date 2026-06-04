from rest_framework import serializers


class FHIRAuxResourceSerializer(serializers.Serializer):
    """Renders a FhirAuxResource (an opaque JSON-blob resource) as a FHIR resource.

    The stored ``fhir_data`` is returned as-is, with ``resourceType`` and the FHIR ``id``
    forced to the server's canonical values (the ``resource_type`` column and the Django
    primary key). No per-field validation is performed -- auxiliary resources are stored
    and returned verbatim.
    """

    def to_representation(self, aux_resource):
        data = dict(aux_resource.fhir_data or {})
        data["resourceType"] = aux_resource.resource_type
        data["id"] = str(aux_resource.pk)
        return data


class FHIRBundledAuxResourceSerializer(serializers.Serializer):
    resource = FHIRAuxResourceSerializer(required=False, read_only=True, source="*")
