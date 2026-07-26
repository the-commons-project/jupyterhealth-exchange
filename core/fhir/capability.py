"""Render the server's CapabilityStatement from the FHIR mapping config.

GET /FHIR/R5/metadata is public (FHIR requires the capabilities interaction to
be retrievable) and exposes only resource-type names, interaction codes, and
search-param names -- all already public in fhir_config.json. The statement
follows the US Core server CapabilityStatement conventions (expectation
extensions, SMART security block) WITHOUT claiming US Core conformance: US
Core targets R4 and requires capabilities JHE does not serve (vread/history,
Provenance _revinclude, US Core profiles, POST search).
"""

from django.utils import timezone

from core.fhir.config import (
    FHIR_VERSION,
    aux_interactions,
    aux_search_params,
    mapped_interactions,
    mapped_search_params,
    supported_resource_types,
)

_EXPECTATION_URL = "http://hl7.org/fhir/StructureDefinition/capabilitystatement-expectation"
_OAUTH_URIS_URL = "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris"
_SECURITY_SERVICE_SYSTEM = "http://terminology.hl7.org/CodeSystem/restful-security-service"
_FHIR_VERSION_NUMBERS = {"R5": "5.0.0"}
# Config interaction name -> FHIR RESTful interaction code (identity when absent).
_INTERACTION_CODES = {"search": "search-type"}
# Config __search value type -> FHIR search-param type (identity when absent).
_SEARCH_TYPES = {"identifier": "token", "code": "token", "const": "token"}


def _shall(element):
    """Attach the US Core expectation extension: everything declared is supported."""
    element["extension"] = [{"url": _EXPECTATION_URL, "valueCode": "SHALL"}]
    return element


def _resource_entry(resource_type):
    interactions = mapped_interactions(resource_type) | aux_interactions(resource_type)
    # Union of both stores' params: a search hits one store, but the client
    # cannot choose which, so "supported" is the union. Mapped specs win ties.
    params = {**aux_search_params(resource_type), **mapped_search_params(resource_type)}
    entry = {"type": resource_type}
    if interactions:
        entry["interaction"] = [_shall({"code": _INTERACTION_CODES.get(i, i)}) for i in sorted(interactions)]
    if params:
        entry["searchParam"] = [
            _shall({"name": name, "type": _SEARCH_TYPES.get(spec["type"], spec["type"])})
            for name, spec in sorted(params.items())
        ]
    return entry


def build_capability_statement(request):
    """The server's CapabilityStatement (R5, kind=instance), derived from fhir_config.json."""
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": timezone.now().isoformat(),
        "kind": "instance",
        "implementation": {
            "description": "JupyterHealth Exchange",
            "url": request.build_absolute_uri(f"/FHIR/{FHIR_VERSION}/"),
        },
        "fhirVersion": _FHIR_VERSION_NUMBERS.get(FHIR_VERSION, FHIR_VERSION),
        "format": ["json"],
        "rest": [
            {
                "mode": "server",
                "security": {
                    "extension": [
                        {
                            "url": _OAUTH_URIS_URL,
                            "extension": [
                                {"url": "authorize", "valueUri": request.build_absolute_uri("/o/authorize/")},
                                {"url": "token", "valueUri": request.build_absolute_uri("/o/token/")},
                            ],
                        }
                    ],
                    "service": [{"coding": [{"system": _SECURITY_SERVICE_SYSTEM, "code": "SMART-on-FHIR"}]}],
                    "description": (
                        "OAuth 2.0 (SMART-on-FHIR). Unauthorized requests are rejected with 401, 403, or 404."
                    ),
                },
                "interaction": [{"code": "batch"}],
                "resource": [_resource_entry(rt) for rt in supported_resource_types()],
            }
        ],
    }
