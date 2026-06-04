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
            practitioner = request.user.practitioner_profile
            if organization_id := request.query_params.get("organization_id"):
                practitioner.save_setting("current_organization_id", int(organization_id))
            if study_id := request.query_params.get("study_id"):
                practitioner.save_setting("current_study_id", int(study_id))
            else:
                practitioner.delete_setting("current_study_id")
        return response
