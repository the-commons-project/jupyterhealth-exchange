"""Validate an incoming FHIR resource dict against its ``fhir.resources`` model.

Used on the write path for auxiliary resources (and anywhere a verbatim FHIR body must be
checked before it is stored). The resource is parsed with the ``fhir.resources`` model that
matches its ``resourceType``; a parse/validation failure is surfaced as a DRF 400.
"""

import importlib

from rest_framework.exceptions import ValidationError as DRFValidationError


def fhir_model_class(resource_type):
    """Return the ``fhir.resources`` model class for a resourceType, or raise LookupError."""
    try:
        module = importlib.import_module(f"fhir.resources.{resource_type.lower()}")
        return getattr(module, resource_type)
    except (ImportError, AttributeError):
        raise LookupError(resource_type)


def validate_fhir_resource(resource_type, data):
    """Validate ``data`` (a FHIR resource dict) against ``resource_type``; raise DRF 400 on failure.

    FHIR JSON is camelCase, so the body is validated as-is. ``resourceType`` in the body is
    ignored in favour of the routed ``resource_type``.
    """
    try:
        model_cls = fhir_model_class(resource_type)
    except LookupError:
        raise DRFValidationError(f"Unknown FHIR resource type: {resource_type}.")

    payload = {key: value for key, value in dict(data).items() if key != "resourceType"}
    try:
        model_cls.parse_obj(payload)
    except Exception as exc:
        raise DRFValidationError(f"Invalid FHIR {resource_type}: {exc}")
