import logging

from rest_framework import permissions

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


# RBAC
ROLE_PERMISSIONS = {
    "manager": [
        "organization.add_practitioner",
        "organization.remove_practitioner",
    ],
    "member": [
        "organization.add_practitioner",
    ],
}


def IfUserCan(resource_and_action: str):
    resource, action = resource_and_action.split(".", 1)

    class _IfUserCan(permissions.IsAuthenticated):

        def has_permission(self, request, view):
            # User has to be authenticated
            if super().has_permission(request, view):
                if link := PractitionerOrganization.objects.filter(
                        practitioner__jhe_user=request.user,
                        organization_id=view.kwargs.get("pk")
                ).first():
                    return f"{resource}.{action}" in ROLE_PERMISSIONS.get(link.role, [])
            return None

    return _IfUserCan
