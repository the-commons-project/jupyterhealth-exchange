"""Loads the FHIR mapping configuration once at import time.

core/fhir/fhir_config.json declares, per resource type, how Django model data maps onto
FHIR fields. It is read a single time when this module is first imported (typically
when the serializers module loads at startup); there is no dynamic reloading.
"""

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent / "fhir_config.json"

with open(_CONFIG_PATH) as f:
    FHIR_CONFIG = json.load(f)

FHIR_VERSION = FHIR_CONFIG.get("fhir_version")

# Both lists are arrays of objects carrying a "resourceType". For mapped resources we index
# by resourceType and strip that key so the stored value is exactly the field mapping the
# engine walks; auxiliary resources contribute only their resourceType.
_MAPPED_RESOURCES = {
    entry["resourceType"]: {key: value for key, value in entry.items() if key != "resourceType"}
    for entry in FHIR_CONFIG.get("mapped_resources", [])
}
_AUX_RESOURCES = frozenset(entry["resourceType"] for entry in FHIR_CONFIG.get("aux_resources", []))


def get_resource_mapping(resource_type):
    """Return the mapping declared for a FHIR resource type (e.g. "Patient").

    Returns an empty dict when the resource type is not configured.
    """
    return _MAPPED_RESOURCES.get(resource_type, {})


def is_mapped_resource(resource_type):
    """True for resources backed by a Django model + field mapping (Patient, Observation)."""
    return resource_type in _MAPPED_RESOURCES


def is_aux_resource(resource_type):
    """True for auxiliary resources stored as opaque JSON blobs (e.g. Condition)."""
    return resource_type in _AUX_RESOURCES


def is_supported_resource(resource_type):
    """True if the resource type is served at all (either mapped or auxiliary)."""
    return is_mapped_resource(resource_type) or is_aux_resource(resource_type)


def aux_resource_types():
    """The set of supported auxiliary (JSON-blob) resource types."""
    return _AUX_RESOURCES
