import inspect
import logging

from rest_framework.viewsets import ModelViewSet

from core.models import Observation
from core.pagination import CustomPageNumberPagination
from core.serializers import ObservationSerializer

logger = logging.getLogger(__name__)


class ObservationViewSet(ModelViewSet):
    model_class = Observation
    serializer_class = ObservationSerializer
    pagination_class = CustomPageNumberPagination
    # Read-only: observations are written through the FHIR endpoints (Observation.fhir_create),
    # which enforce the patient's consent for the code and require a Device reference. Neither
    # check exists here, and get_queryset's organization scoping is the only authorization on
    # this path -- it does not consult the practitioner's role, so leaving write verbs routed
    # would let any organization member (a `viewer` included) rewrite or delete clinical data.
    # Kept as a ModelViewSet so the verbs can be restored by widening this list once the
    # authorization the FHIR path applies is implemented here too.
    http_method_names = ["get", "head", "options"]

    supported_query_params = {
        key
        for key in inspect.signature(Observation.for_practitioner_organization_study_patient).parameters
        if key not in {"jhe_user_id"}
    }

    def get_queryset(self):
        return Observation.for_practitioner_organization_study_patient(
            self.request.user.id,
            **{key: value for key, value in self.request.query_params.items() if key in self.supported_query_params},
        )

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        if hasattr(request.user, "practitioner_profile"):
            organization_id = request.query_params.get("organization_id")
            study_id = request.query_params.get("study_id")
            request.user.practitioner_profile.remember_settings(
                save={
                    "current_organization_id": int(organization_id) if organization_id else None,
                    "current_study_id": int(study_id) if study_id else None,
                },
                # An absent study is the "All Studies" selection, so it is forgotten rather
                # than left stale; an absent organization just was not part of this request.
                forget=("current_study_id",),
            )
        return response
