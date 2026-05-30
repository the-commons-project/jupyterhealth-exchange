"""Loads the FHIR data-mapping configuration once at import time.

jhe/fhir_config.json declares, per resource type, how Django model data maps onto
FHIR fields. It is read a single time when this module is first imported (typically
when the serializers module loads at startup); there is no dynamic reloading.
"""

import json
from pathlib import Path

from django.conf import settings

_CONFIG_PATH = Path(settings.BASE_DIR) / "jhe" / "fhir_config.json"

with open(_CONFIG_PATH) as f:
    FHIR_CONFIG = json.load(f)

FHIR_VERSION = FHIR_CONFIG.get("fhir_version")
_RESOURCE_MAPPINGS = FHIR_CONFIG.get("data_mapping", {}).get("resources", {})


def get_resource_mapping(resource_type):
    """Return the data_mapping declared for a FHIR resource type (e.g. "Patient").

    Returns an empty dict when the resource type is not configured.
    """
    return _RESOURCE_MAPPINGS.get(resource_type, {})
