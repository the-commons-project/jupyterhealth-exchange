import logging

from django.db.models import ProtectedError
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet

from core.models import EhrVendor
from core.pagination import CustomPageNumberPagination
from core.permissions import IsSuperUser
from core.serializers import EhrVendorSerializer

logger = logging.getLogger(__name__)


class EhrVendorViewSet(ModelViewSet):
    serializer_class = EhrVendorSerializer
    queryset = EhrVendor.objects.all().order_by("name").prefetch_related("brands__locations")
    permission_classes = [IsAuthenticated, IsSuperUser]
    pagination_class = CustomPageNumberPagination

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            raise ValidationError("Cannot delete a vendor that still has EHR brands under it.")
