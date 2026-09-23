import logging

from django.db.models import ProtectedError
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet

from core.models import EhrVendor
from core.pagination import CustomPageNumberPagination
from core.permissions import IsSuperUser
from core.serializers import EhrVendorListSerializer, EhrVendorSerializer

logger = logging.getLogger(__name__)


class EhrVendorViewSet(ModelViewSet):
    serializer_class = EhrVendorSerializer
    queryset = EhrVendor.objects.all().order_by("name")
    permission_classes = [IsAuthenticated, IsSuperUser]
    pagination_class = CustomPageNumberPagination

    def get_serializer_class(self):
        # The list view's table never shows brands/locations, and a vendor can have thousands of
        # them (the real Epic import), so list skips that nested serialization entirely; only a
        # single vendor's detail view (retrieve) needs it.
        if self.action == "list":
            return EhrVendorListSerializer
        return EhrVendorSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == "retrieve":
            queryset = queryset.prefetch_related("brands__locations")
        return queryset

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            raise ValidationError("Cannot delete a vendor that still has EHR brands under it.")
