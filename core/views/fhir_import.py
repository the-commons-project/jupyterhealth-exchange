"""R4 ingestion endpoint.

``POST /fhir-import/R4/<resource>`` accepts a single R4 FHIR resource; ``POST /fhir-import/R4``
accepts a Bundle of R4 resources. Each body is converted to R5 by the cross-version engine
(:mod:`core.fhir.cross_version`) and then handed to the *normal* create routing on
:class:`~core.views.fhir.FHIRResourceView` -- so mapped-vs-aux routing, R5 validation, the
``X-JHE-FHIR-Source-ID`` write context, and JHE provenance stamping are all reused unchanged.

The conversion is best-effort and lossy (see ``fhir-r4-import.md`` at the repo root); R5 validation on the
create path is the gate that rejects anything the transform could not produce cleanly.
"""

from rest_framework import status as http_status
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from core.fhir.cross_version import XVerError, transform_to_r5

from .fhir import FHIRResourceView, _camelized


class FHIRImportView(FHIRResourceView):
    """Create-only R4 endpoint: convert R4 -> R5, then delegate to the normal create routing."""

    # Import is create-only; the inherited read/update/delete verbs are refused.
    def get(self, request, *args, **kwargs):
        raise MethodNotAllowed("GET", detail="The R4 import endpoint only supports create (POST).")

    def put(self, request, *args, **kwargs):
        raise MethodNotAllowed("PUT", detail="The R4 import endpoint only supports create (POST).")

    def patch(self, request, *args, **kwargs):
        raise MethodNotAllowed("PATCH", detail="The R4 import endpoint only supports create (POST).")

    def delete(self, request, *args, **kwargs):
        raise MethodNotAllowed("DELETE", detail="The R4 import endpoint only supports create (POST).")

    def post(self, request, resource=None, id=None):
        if resource is None:
            return self._import_bundle(request.data)
        if id is not None:
            raise MethodNotAllowed("POST")
        self._check_supported(resource)
        created = self._create(resource, self._to_r5(resource, request.data))
        return Response(created, status=http_status.HTTP_201_CREATED)

    # -- helpers --

    def _to_r5(self, resource_type, data):
        """Camelize (the parser snake-cases incoming JSON) and transform R4 -> R5."""
        try:
            return transform_to_r5(resource_type, _camelized(data))
        except XVerError as exc:
            raise DRFValidationError(str(exc))

    def _import_bundle(self, data):
        bundle = _camelized(data)
        if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
            raise DRFValidationError("Expected a Bundle at /fhir-import/R4.")

        entries = []
        for entry in bundle.get("entry", []) or []:
            resource = (entry or {}).get("resource") or {}
            resource_type = resource.get("resourceType")
            try:
                if not resource_type:
                    raise DRFValidationError("Bundle entry is missing a resource / resourceType.")
                self._check_supported(resource_type)
                created = self._create(resource_type, self._to_r5(resource_type, resource))
                entries.append({"response": {"status": "201 Created"}, "resource": created})
            except Exception as exc:  # per-entry best-effort, mirroring batch semantics.
                entries.append(_error_entry(exc))

        # Entries are processed independently (batch semantics), even if the request Bundle
        # declared itself a transaction -- the conversion is best-effort and not atomic.
        return Response(
            {"resourceType": "Bundle", "type": "batch-response", "entry": entries},
            status=http_status.HTTP_200_OK,
        )


def _error_entry(exc):
    detail = getattr(exc, "detail", None) or str(exc)
    code = getattr(exc, "status_code", http_status.HTTP_400_BAD_REQUEST)
    return {
        "response": {
            "status": f"{code} {getattr(exc, 'default_code', 'error')}",
            "outcome": {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "processing", "diagnostics": str(detail)}],
            },
        }
    }
