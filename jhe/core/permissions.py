from rest_framework import permissions
import logging

from core.models import PractitionerOrganization


logger = logging.getLogger(__name__)

class IsSelfUrlPath(permissions.BasePermission):

    def has_permission(self, request, view):
        return int(request.parser_context['kwargs']['pk']) == request.user.id


class IsOrganizationManager(permissions.IsAuthenticated):
    """
    Only allows organization managers to add and delete the practitioners.
    """

    def has_permission(self, request, view):
        """
        Return True if the user is an organization manager or False if not.
        Also checks the authenticated user.
        """

        if super().has_permission(request, view):
            if org_id := view.kwargs.get('pk'):
                return PractitionerOrganization.objects.filter(
                    practitioner__jhe_user=request.user,
                    organization_id=org_id,
                    role=PractitionerOrganization.ROLE_MANAGER
                ).exists()
        return False
