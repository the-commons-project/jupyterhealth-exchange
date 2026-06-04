"""Unified FHIR R5 resource endpoint.

A single view, ``FHIRResourceView``, serves every supported FHIR resource at
``FHIR/<version>/<resource>`` (and ``.../<resource>/<id>``), where ``<version>`` comes
from core/fhir/fhir_config.json (e.g. ``FHIR/R5/Patient``); the lowercase ``fhir/r5/``
path is kept as a backward-compatible alias. It consults the config to decide how a
resource is backed:

  * **mapped** resources (Patient, Observation) are projected onto Django models via
    the field mapping -- searches and reads run the existing ORM queries and the
    config-driven serializers; writes split the payload into mapped columns plus a
    leftover ``aux_fhir_data`` blob (see core.fhir.engine.split_resource), preserving
    each resource's domain validation.
  * **auxiliary** resources (e.g. Condition, QuestionnaireResponse) are opaque JSON
    blobs stored in the generic FhirAuxResource model with full CRUD and no per-field logic.

The FHIR bundle batch endpoint (POST at the base, e.g. ``FHIR/R5/``) remains served by FHIRBase.
"""

import logging
import uuid

from django.core.exceptions import BadRequest as DjangoBadRequest
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db.utils import IntegrityError
from rest_framework import status as http_status
from rest_framework.exceptions import APIException, MethodNotAllowed, NotFound
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from core.fhir.config import (
    get_resource_mapping,
    is_aux_resource,
    is_mapped_resource,
    is_supported_resource,
)
from core.fhir.engine import get_mapping_interactions
from core.fhir.pagination import FHIRBundlePagination
from core.models import FhirAuxResource, Observation, Patient, Study
from core.serializers import FHIRAuxResourceSerializer, FHIRObservationSerializer, FHIRPatientSerializer
from core.views.fhir_base import FHIRBase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resource handlers
# ---------------------------------------------------------------------------


class FHIRResourceHandler:
    """Per-resource strategy. Subclasses implement the operations they support.

    ``search`` returns a queryset (the view paginates it and serializes each row via
    ``serialize``); ``read``/``create``/``update`` return a single FHIR resource dict;
    ``delete`` returns nothing. Unsupported operations raise ``MethodNotAllowed``.
    """

    def __init__(self, resource_type, request):
        self.resource_type = resource_type
        self.request = request
        self.user = request.user

    def serialize(self, instance):
        raise NotImplementedError

    def search(self):
        raise MethodNotAllowed("GET", detail=f"Search is not supported for {self.resource_type}.")

    def read(self, fhir_id):
        raise MethodNotAllowed("GET", detail=f"Read is not supported for {self.resource_type}.")

    def create(self, data):
        raise MethodNotAllowed("POST", detail=f"Create is not supported for {self.resource_type}.")

    def update(self, fhir_id, data, partial=False):
        verb = "PATCH" if partial else "PUT"
        raise MethodNotAllowed(verb, detail=f"Update is not supported for {self.resource_type}.")

    def delete(self, fhir_id):
        raise MethodNotAllowed("DELETE", detail=f"Delete is not supported for {self.resource_type}.")


class ObservationHandler(FHIRResourceHandler):
    """Observation: config-driven read/search; create preserves the full domain
    validation in Observation.fhir_create (consent, device, base64, identifiers)."""

    def serialize(self, instance):
        return FHIRObservationSerializer().to_representation(instance)

    def search(self):
        request = self.request
        # GET /Observation?patient._has:Group:member:_id=<group-id>
        study_id = request.GET.get("patient._has:_group:member:_id", None)
        if study_id is None:  # TBD: remove this
            study_id = request.GET.get("_has:_group:member:_id", None)

        patient_id = request.GET.get("patient", None)
        patient_identifier_system_and_value = request.GET.get("patient.identifier", None)
        coding_system_and_value = request.GET.get("code", None)

        if not (study_id or patient_id or patient_identifier_system_and_value):
            raise DRFValidationError(
                "Request parameter patient._has:Group:member:_id=<study_id> or"
                " patient=<patient_id> or patient.identifier=<system>|<value> must be provided."
            )

        if study_id and (not Study.practitioner_authorized(self.user.id, study_id)):
            raise DRFPermissionDenied("Current User does not have authorization to access this Study.")

        if study_id and patient_id and (not Study.has_patient(study_id, patient_id)):
            raise DRFValidationError("The requested Patient is not part of the specified Study.")

        coding_system = None
        coding_value = None
        if coding_system_and_value:
            coding_system, _, coding_value = coding_system_and_value.partition("|")

        patient_identifier_system = None
        patient_identifier_value = None
        if patient_identifier_system_and_value:
            patient_identifier_system, _, patient_identifier_value = patient_identifier_system_and_value.partition("|")

        return Observation.fhir_search(
            self.user.id,
            study_id=study_id,
            patient_id=patient_id,
            patient_identifier_system=patient_identifier_system,
            patient_identifier_value=patient_identifier_value,
            coding_system=coding_system,
            coding_code=coding_value,
        )

    def read(self, fhir_id):
        observation = Observation.fhir_search(self.user.id, observation_id=fhir_id).first()
        if observation is None:
            raise NotFound(f"Observation/{fhir_id} not found.")
        return self.serialize(observation)

    def create(self, data):
        observation = Observation.fhir_create(data, self.user)
        logger.debug("created observation: %s", observation)

        # Patients have no Practitioner record, so fhir_search (which requires one) would
        # 404. For the create response we only need the single observation just persisted;
        # FHIRObservationSerializer expects the related rows the search prefetches, so build
        # a minimal FHIR-compliant response for the patient path.
        if self.user.is_patient():
            obs = Observation.objects.select_related("subject_patient", "codeable_concept").get(pk=observation.id)
            return {
                "resourceType": "Observation",
                "id": str(obs.id),
                "status": "final",
                "meta": {"lastUpdated": obs.last_updated.isoformat() if obs.last_updated else None},
                "subject": {"reference": f"Patient/{obs.subject_patient_id}"},
                "code": {
                    "coding": [{"system": obs.codeable_concept.coding_system, "code": obs.codeable_concept.coding_code}]
                },
            }

        fhir_observation = Observation.fhir_search(self.user.id, observation_id=observation.id).first()
        return self.serialize(fhir_observation)


class PatientHandler(FHIRResourceHandler):
    """Patient: config-driven read/search; create reverse-maps the payload into model
    columns (via the engine) plus an aux_fhir_data blob, and links the JheUser/identifiers."""

    def serialize(self, instance):
        return FHIRPatientSerializer().to_representation(instance)

    def search(self):
        request = self.request
        patient_identifier_system_and_value = request.GET.get("identifier", None)

        # GET /Patient?_has:Group:member:_id=<group-id>
        study_id = request.GET.get("_has:_group:member:_id", None)

        if not (study_id or patient_identifier_system_and_value):
            raise DRFValidationError(
                "Request parameter _has:Group:member:_id=<study_id> or"
                " patient.identifier=<system>|<value> must be provided."
            )

        patient_identifier_system = None
        patient_identifier_value = None
        if patient_identifier_system_and_value:
            patient_identifier_split = patient_identifier_system_and_value.split("|")
            patient_identifier_system = patient_identifier_split[0]
            patient_identifier_value = patient_identifier_split[1]

        if study_id and (not Study.practitioner_authorized(self.user.id, study_id)):
            raise DRFPermissionDenied("Current User does not have authorization to access this Study.")

        if patient_identifier_system_and_value and (
            not Patient.practitioner_authorized(self.user.id, None, None, patient_identifier_value)
        ):
            raise DRFPermissionDenied("Current User does not have authorization to access this Patient.")

        return Patient.fhir_search(self.user.id, study_id, patient_identifier_system, patient_identifier_value)

    def read(self, fhir_id):
        patient = Patient.for_practitioner_organization_study(self.user.id, patient_id=fhir_id).first()
        if patient is None:
            raise NotFound(f"Patient/{fhir_id} not found.")
        return self.serialize(patient)

    def create(self, data):
        patient = Patient.fhir_create(data, self.user)
        return self.serialize(patient)


class AuxResourceHandler(FHIRResourceHandler):
    """Auxiliary resources: opaque JSON-blob CRUD with no per-field computation."""

    def _scoped_queryset(self):
        if self.user.is_patient():
            return FhirAuxResource.for_patient(self.user.get_patient(), self.resource_type)
        return FhirAuxResource.fhir_search(self.user.id, self.resource_type)

    def _get_instance(self, fhir_id):
        try:
            return self._scoped_queryset().get(pk=fhir_id)
        except (FhirAuxResource.DoesNotExist, ValueError, TypeError):
            raise NotFound(f"{self.resource_type}/{fhir_id} not found.")

    def serialize(self, instance):
        return FHIRAuxResourceSerializer().to_representation(instance)

    def search(self):
        return self._scoped_queryset()

    def read(self, fhir_id):
        return self.serialize(self._get_instance(fhir_id))

    def create(self, data):
        return self.serialize(self._persist(FhirAuxResource(resource_type=self.resource_type), data))

    def update(self, fhir_id, data, partial=False):
        instance = self._get_instance(fhir_id)
        body = self._body(data)
        if partial:
            merged = dict(instance.fhir_data or {})
            merged.update(body)
            body = merged
        return self.serialize(self._persist(instance, body))

    def delete(self, fhir_id):
        self._get_instance(fhir_id).delete()

    # -- persistence helpers --

    @staticmethod
    def _body(data):
        # Store the FHIR body verbatim minus resourceType (it is derived from the column).
        return {key: value for key, value in dict(data).items() if key != "resourceType"}

    def _persist(self, instance, body):
        body = self._body(body)
        patient, patient_fhir_id = self._resolve_patient(body)
        meta = body.get("meta")
        instance.resource_type = self.resource_type
        instance.patient = patient
        instance.patient_fhir_id = patient_fhir_id
        instance.fhir_resource_id = str(body.get("id") or instance.fhir_resource_id or uuid.uuid4())
        instance.source = meta.get("source") if isinstance(meta, dict) else None
        instance.fhir_data = body
        instance.save()
        return instance

    def _resolve_patient(self, body):
        reference = None
        for key in ("subject", "patient"):
            node = body.get(key)
            if isinstance(node, dict) and isinstance(node.get("reference"), str):
                reference = node["reference"]
                break

        if not reference or not reference.startswith("Patient/"):
            # No patient linkage in the payload. A patient user still owns what they create.
            if self.user.is_patient():
                return self.user.get_patient(), None
            return None, None

        patient_fhir_id = reference.split("/", 1)[1]
        try:
            patient = Patient.objects.get(pk=patient_fhir_id)
        except (Patient.DoesNotExist, ValueError, TypeError):
            raise DRFValidationError(f"Referenced Patient/{patient_fhir_id} does not exist.")
        self._authorize_patient(patient)
        return patient, patient_fhir_id

    def _authorize_patient(self, patient):
        if self.user.is_practitioner():
            if not Patient.practitioner_authorized(self.user.id, patient.id):
                raise DRFPermissionDenied("Current user does not have access to the referenced Patient.")
        else:
            own = self.user.get_patient()
            if not own or own.id != patient.id:
                raise DRFPermissionDenied("The referenced Patient does not match the current user.")


_MAPPED_HANDLERS = {"Observation": ObservationHandler, "Patient": PatientHandler}


class _NotImplementedResource(APIException):
    status_code = http_status.HTTP_501_NOT_IMPLEMENTED
    default_detail = "This FHIR resource type is configured but not yet served."


def get_handler(resource_type, request):
    if is_mapped_resource(resource_type):
        handler_cls = _MAPPED_HANDLERS.get(resource_type)
        if handler_cls is None:
            # Declared in the config (so its interactions are known) but no handler is wired
            # up yet -- a clean 501 beats a KeyError 500.
            raise _NotImplementedResource(f"The {resource_type} resource type is configured but not yet served.")
        return handler_cls(resource_type, request)
    if is_aux_resource(resource_type):
        return AuxResourceHandler(resource_type, request)
    raise NotFound(f"Unsupported FHIR resource type: {resource_type}.")


# ---------------------------------------------------------------------------
# The unified view
# ---------------------------------------------------------------------------


class FHIRResourceView(APIView):
    """Dispatches an HTTP verb on ``FHIR/<version>/<resource>[/<id>]`` to the resource handler.

    The HTTP request maps to a FHIR interaction (search/read/create/update/delete); if the
    resource's ``__interaction`` allow-list in the config omits it, the request is refused
    with 405 before any handler runs. A missing ``__interaction`` allows every interaction.
    """

    def _handler(self, resource):
        if not is_supported_resource(resource):
            raise NotFound(f"Unsupported FHIR resource type: {resource}.")
        return get_handler(resource, self.request)

    def _enforce_interaction(self, resource, interaction):
        # Refuse a disallowed interaction up front (405). resources not in the mapped config
        # (auxiliary types, or unknown types) have no allow-list, so all interactions pass.
        allowed = get_mapping_interactions(get_resource_mapping(resource))
        if allowed is not None and interaction not in allowed:
            raise MethodNotAllowed(
                self.request.method, detail=f"The '{interaction}' interaction is not allowed for {resource}."
            )

    def get(self, request, resource, id=None):
        self._enforce_interaction(resource, "read" if id is not None else "search")
        handler = self._handler(resource)
        if id is None:
            return self._search_bundle(handler)
        return Response(handler.read(id))

    def post(self, request, resource, id=None):
        self._enforce_interaction(resource, "search" if id == "_search" else "create")
        handler = self._handler(resource)
        if id == "_search":  # FHIR search via POST
            return self._search_bundle(handler)
        if id is not None:
            raise MethodNotAllowed("POST")
        return Response(handler.create(request.data), status=http_status.HTTP_201_CREATED)

    def put(self, request, resource, id=None):
        if id is None:
            raise MethodNotAllowed("PUT", detail="An id is required to update a resource.")
        self._enforce_interaction(resource, "update")
        return Response(self._handler(resource).update(id, request.data, partial=False))

    def patch(self, request, resource, id=None):
        if id is None:
            raise MethodNotAllowed("PATCH", detail="An id is required to update a resource.")
        self._enforce_interaction(resource, "update")
        return Response(self._handler(resource).update(id, request.data, partial=True))

    def delete(self, request, resource, id=None):
        if id is None:
            raise MethodNotAllowed("DELETE", detail="An id is required to delete a resource.")
        self._enforce_interaction(resource, "delete")
        self._handler(resource).delete(id)
        return Response(status=http_status.HTTP_204_NO_CONTENT)

    def _search_bundle(self, handler):
        paginator = FHIRBundlePagination()
        page = paginator.paginate_queryset(handler.search(), self.request, view=self)
        entries = [{"resource": handler.serialize(obj)} for obj in page]
        return paginator.get_paginated_response(entries)

    def handle_exception(self, exc):
        """Render domain/model exceptions as FHIR OperationOutcome with the right status."""
        if isinstance(exc, (DjangoPermissionDenied, DRFPermissionDenied)):
            return self._outcome(http_status.HTTP_403_FORBIDDEN, exc)
        if isinstance(exc, NotFound):
            return self._outcome(http_status.HTTP_404_NOT_FOUND, exc)
        if isinstance(exc, MethodNotAllowed):
            return self._outcome(http_status.HTTP_405_METHOD_NOT_ALLOWED, exc)
        if isinstance(exc, IntegrityError):
            return self._outcome(http_status.HTTP_409_CONFLICT, exc)
        if isinstance(exc, (DjangoBadRequest, DRFValidationError)):
            return self._outcome(http_status.HTTP_400_BAD_REQUEST, exc)
        if isinstance(exc, APIException):
            # Any other DRF exception (e.g. the 501 not-implemented) already carries the
            # correct status; render it as an OperationOutcome rather than DRF's default JSON.
            status_code = exc.status_code if isinstance(exc.status_code, int) else http_status.HTTP_400_BAD_REQUEST
            return self._outcome(status_code, exc)
        logger.exception("Unhandled error in FHIR resource view")
        return self._outcome(http_status.HTTP_422_UNPROCESSABLE_ENTITY, exc)

    @staticmethod
    def _outcome(status_code, exc):
        return Response(FHIRBase.error_outcome(str(exc)), status=status_code)
